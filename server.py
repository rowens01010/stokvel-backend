from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends, Cookie, Header
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from supabase import create_client, Client
import os
import logging
import hashlib
import uuid
import random
import httpx
from urllib.parse import quote
from pathlib import Path
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timezone, timedelta
from stellar_sdk import Server, Keypair, TransactionBuilder, Network, Asset
from stellar_sdk.exceptions import NotFoundError
import asyncio


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_SERVICE_ROLE_KEY']
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Stellar configuration
STELLAR_NETWORK = os.environ.get("STELLAR_NETWORK", "TESTNET")
STELLAR_HORIZON_URL = "https://horizon-testnet.stellar.org" if STELLAR_NETWORK == "TESTNET" else "https://horizon.stellar.org"
STELLAR_NETWORK_PASSPHRASE = Network.TESTNET_NETWORK_PASSPHRASE if STELLAR_NETWORK == "TESTNET" else Network.PUBLIC_NETWORK_PASSPHRASE
STELLAR_SERVER = Server(STELLAR_HORIZON_URL)

# Stokvel Treasury (for receiving upgrade payments)
STOKVEL_TREASURY_SECRET = os.environ.get("STOKVEL_TREASURY_SECRET", "")
STOKVEL_TREASURY_PUBLIC = os.environ.get("STOKVEL_TREASURY_PUBLIC", "")

app = FastAPI()
api_router = APIRouter(prefix="/api")

FREE_CARD_TTL_MINUTES = 20
PREMIUM_CARD_TTL_MINUTES = 35
VOTES_PER_TOKEN = 10
INITIAL_TOKENS = 3
DIAMOND_BOOST_COST = 5
DIAMOND_BOOST_MINUTES = 10

# Stellar constants
UPGRADE_COST_XLM = 20.0
VOTE_COST_XLM = 0.07
TOKENS_PER_VOTE = 1
TOKENS_TO_CREATE_CARD = 5
BUILT_IN_CARD_LIMIT = 20
FREE_CARDS_PER_USER = 1

# Time marketplace constants
DEFAULT_TIME_PRICE_XLM = 0.05
TIME_PURCHASE_MIN_MINUTES = 10
TIME_PURCHASE_MAX_MINUTES = 60

# Track built-in cards replaced
built_in_cards_replaced = 0


# ========= Helpers =========
def _parse_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = value.replace("Z", "+00:00") if isinstance(value, str) else value
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _maybe(res):
    if res is None:
        return None
    return getattr(res, "data", None)


def _check_stellar_balance(public_key: str) -> float:
    try:
        account = STELLAR_SERVER.accounts().account_id(public_key).call()
        for balance in account['balances']:
            if balance['asset_type'] == 'native':
                return float(balance['balance'])
        return 0.0
    except NotFoundError:
        return 0.0
    except Exception as e:
        logger.error(f"Error checking balance: {e}")
        return 0.0


def _create_stellar_keypair():
    keypair = Keypair.random()
    return {
        "public_key": keypair.public_key,
        "secret": keypair.secret
    }


def _get_free_cards_count(user_id: str) -> int:
    res = _maybe(sb.table("free_cards_used").select("*").eq("user_id", user_id).maybe_single().execute())
    if res:
        return res.get("cards_created", 0)
    return 0


# ========= Models =========
class CardCreate(BaseModel):
    image_url: str
    smart_link: str
    title: Optional[str] = ""
    use_diamond_boost: Optional[bool] = False


class PayfastInitiatePayload(BaseModel):
    return_url: str
    cancel_url: str


class GoogleAuthPayload(BaseModel):
    id_token: str
    email: str
    name: str
    picture: str
    ref: Optional[str] = None


class StellarUpgradePayload(BaseModel):
    transaction_hash: str


class StellarVotePayload(BaseModel):
    transaction_hash: str


class TimeListingCreate(BaseModel):
    minutes_available: int
    price_per_minute_xlm: float = DEFAULT_TIME_PRICE_XLM


class TimePurchasePayload(BaseModel):
    listing_id: str
    minutes_to_buy: int
    card_id: str
    transaction_hash: str


# ========= Auth =========
def get_current_user(
    request: Request,
    session_token_cookie: Optional[str] = Cookie(default=None, alias="session_token"),
    authorization: Optional[str] = Header(default=None),
) -> dict:
    token = session_token_cookie
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    res = sb.table("user_sessions").select("*").eq("session_token", token).maybe_single().execute()
    session = _maybe(res)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid session")

    expires_at = _parse_dt(session["expires_at"])
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Session expired")

    user_res = sb.table("users").select("*").eq("user_id", session["user_id"]).maybe_single().execute()
    user = _maybe(user_res)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


# ========= Root Routes =========
@app.get("/")
def root():
    return {"message": "Stokvel API is running"}


@api_router.get("/")
def api_root():
    return {"message": "Stokvel API"}


@api_router.post("/auth/google")
def auth_google(payload: GoogleAuthPayload, response: Response):
    email = payload.email
    name = payload.name
    picture = payload.picture
    ref = payload.ref
    
    session_token = f"session_{uuid.uuid4().hex[:32]}"
    
    existing = _maybe(sb.table("users").select("*").eq("email", email).maybe_single().execute())
    now_iso = datetime.now(timezone.utc).isoformat()

    if existing:
        user_id = existing["user_id"]
        updates = {"name": name, "picture": picture}
        if not existing.get("referral_code"):
            updates["referral_code"] = uuid.uuid4().hex[:8]
        if existing.get("diamonds") is None:
            updates["diamonds"] = 0
        if existing.get("membership_type") is None:
            updates["membership_type"] = "free"
        if existing.get("has_paid_upgrade") is None:
            updates["has_paid_upgrade"] = False
        sb.table("users").update(updates).eq("user_id", user_id).execute()
    else:
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        referral_code = uuid.uuid4().hex[:8]
        referred_by = None
        if ref:
            ref_user = _maybe(sb.table("users").select("*").eq("referral_code", ref).maybe_single().execute())
            if ref_user and ref_user["user_id"] != user_id:
                referred_by = ref_user["user_id"]
                sb.table("users").update(
                    {"diamonds": (ref_user.get("diamonds") or 0) + 1}
                ).eq("user_id", ref_user["user_id"]).execute()
        
        stellar_keypair = _create_stellar_keypair()
        
        sb.table("users").insert({
            "user_id": user_id,
            "email": email,
            "name": name,
            "picture": picture,
            "tokens": INITIAL_TOKENS,
            "diamonds": 0,
            "is_premium": False,
            "premium_until": None,
            "votes_since_token": 0,
            "referral_code": referral_code,
            "referred_by": referred_by,
            "membership_type": "free",
            "has_paid_upgrade": False,
            "stellar_public_key": stellar_keypair["public_key"],
            "stellar_seed": stellar_keypair["secret"],
            "created_at": now_iso,
        }).execute()
        
        sb.table("free_cards_used").insert({
            "user_id": user_id,
            "cards_created": 0,
        }).execute()

    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    sb.table("user_sessions").upsert({
        "session_token": session_token,
        "user_id": user_id,
        "expires_at": expires_at.isoformat(),
        "created_at": now_iso,
    }).execute()

    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        secure=False,
        samesite="lax",
        path="/",
        max_age=7 * 24 * 60 * 60,
    )
    return {"ok": True, "user_id": user_id, "token": session_token}


@api_router.get("/auth/me")
def auth_me(user: dict = Depends(get_current_user)):
    if user.get("is_premium") and user.get("premium_until"):
        pu = _parse_dt(user["premium_until"])
        if pu and pu < datetime.now(timezone.utc):
            sb.table("users").update({"is_premium": False}).eq("user_id", user["user_id"]).execute()
            user["is_premium"] = False
    
    xlm_balance = 0.0
    if user.get("stellar_public_key"):
        xlm_balance = _check_stellar_balance(user["stellar_public_key"])
    
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "name": user["name"],
        "picture": user.get("picture", ""),
        "tokens": user.get("tokens", 0),
        "diamonds": user.get("diamonds", 0),
        "is_premium": user.get("is_premium", False),
        "premium_until": user.get("premium_until"),
        "votes_since_token": user.get("votes_since_token", 0),
        "votes_per_token": VOTES_PER_TOKEN,
        "referral_code": user.get("referral_code"),
        "diamond_boost_cost": DIAMOND_BOOST_COST,
        "diamond_boost_minutes": DIAMOND_BOOST_MINUTES,
        "membership_type": user.get("membership_type", "free"),
        "has_paid_upgrade": user.get("has_paid_upgrade", False),
        "stellar_public_key": user.get("stellar_public_key"),
        "xlm_balance": xlm_balance,
    }


@api_router.post("/auth/logout")
def auth_logout(
    response: Response,
    session_token_cookie: Optional[str] = Cookie(default=None, alias="session_token"),
    authorization: Optional[str] = Header(default=None),
):
    token = session_token_cookie
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1]
    if token:
        sb.table("user_sessions").delete().eq("session_token", token).execute()
    response.delete_cookie(key="session_token", path="/", samesite="lax", secure=False)
    return {"ok": True}


# ========= Referral =========
@api_router.get("/referral/me")
def referral_me(user: dict = Depends(get_current_user)):
    return {
        "referral_code": user.get("referral_code"),
        "diamonds": user.get("diamonds", 0),
        "diamond_boost_cost": DIAMOND_BOOST_COST,
        "diamond_boost_minutes": DIAMOND_BOOST_MINUTES,
    }


# ========= Cards =========
def _card_public(doc: dict) -> dict:
    return {
        "card_id": doc["card_id"],
        "owner_id": doc["owner_id"],
        "owner_name": doc.get("owner_name", ""),
        "image_url": doc["image_url"],
        "smart_link": doc["smart_link"],
        "title": doc.get("title", ""),
        "votes": doc.get("votes", 0),
        "created_at": doc["created_at"],
        "expires_at": doc["expires_at"],
        "is_premium": doc.get("is_premium", False),
        "diamond_boosted": doc.get("diamond_boosted", False),
        "owner_stellar_wallet": doc.get("owner_stellar_wallet"),
        "vote_cost_xlm": doc.get("vote_cost_xlm", 0.07),
        "time_extensions": doc.get("time_extensions", 0),
        "total_time_purchased": doc.get("total_time_purchased", 0),
    }


@api_router.post("/cards")
def create_card(payload: CardCreate, user: dict = Depends(get_current_user)):
    free_cards_used = _get_free_cards_count(user["user_id"])
    is_free_card = free_cards_used < FREE_CARDS_PER_USER
    
    if not is_free_card:
        if user.get("tokens", 0) < TOKENS_TO_CREATE_CARD:
            raise HTTPException(status_code=402, detail=f"Not enough tokens. Need {TOKENS_TO_CREATE_CARD} tokens.")
    
    if not payload.smart_link.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="smart_link must be a valid URL")
    if not payload.image_url:
        raise HTTPException(status_code=400, detail="image_url is required")

    use_boost = bool(payload.use_diamond_boost)
    if use_boost and user.get("diamonds", 0) < DIAMOND_BOOST_COST:
        raise HTTPException(status_code=400, detail=f"Need {DIAMOND_BOOST_COST} diamonds to boost")

    base_ttl = PREMIUM_CARD_TTL_MINUTES if user.get("is_premium") else FREE_CARD_TTL_MINUTES
    ttl = base_ttl + (DIAMOND_BOOST_MINUTES if use_boost else 0)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=ttl)
    
    card = {
        "card_id": f"card_{uuid.uuid4().hex[:12]}",
        "owner_id": user["user_id"],
        "owner_name": user.get("name", ""),
        "image_url": payload.image_url,
        "smart_link": payload.smart_link,
        "title": payload.title or "",
        "votes": 0,
        "created_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "is_premium": bool(user.get("is_premium", False)),
        "diamond_boosted": use_boost,
        "owner_stellar_wallet": user.get("stellar_public_key"),
        "vote_cost_xlm": VOTE_COST_XLM,
        "time_extensions": 0,
        "total_time_purchased": 0,
    }
    sb.table("cards").insert(card).execute()

    if is_free_card:
        sb.table("free_cards_used").upsert({
            "user_id": user["user_id"],
            "cards_created": free_cards_used + 1,
        }).execute()
    else:
        new_tokens = user["tokens"] - TOKENS_TO_CREATE_CARD
        new_diamonds = user.get("diamonds", 0) - (DIAMOND_BOOST_COST if use_boost else 0)
        sb.table("users").update({"tokens": new_tokens, "diamonds": new_diamonds}).eq("user_id", user["user_id"]).execute()
    
    return _card_public(card)


@api_router.get("/cards/marketplace")
def get_marketplace(user: dict = Depends(get_current_user)):
    now_iso = datetime.now(timezone.utc).isoformat()
    res = sb.table("cards").select("*").gt("expires_at", now_iso).neq("owner_id", user["user_id"]).limit(500).execute()
    cards = res.data or []
    random.shuffle(cards)
    
    marketplace_cards = [_card_public(c) for c in cards[:12]]
    
    total_cards_res = sb.table("cards").select("card_id", count="exact").execute()
    total_user_cards = total_cards_res.count if hasattr(total_cards_res, 'count') else len(total_cards_res.data or [])
    
    if len(marketplace_cards) < 12 and total_user_cards < BUILT_IN_CARD_LIMIT:
        built_in_needed = 12 - len(marketplace_cards)
        marketplace_cards.extend(_get_built_in_cards(built_in_needed))
    
    return marketplace_cards[:12]


@api_router.get("/cards/mine")
def get_my_cards(user: dict = Depends(get_current_user)):
    res = sb.table("cards").select("*").eq("owner_id", user["user_id"]).order("created_at", desc=True).limit(500).execute()
    return [_card_public(c) for c in (res.data or [])]


@api_router.post("/cards/{card_id}/vote")
def vote_card(card_id: str, user: dict = Depends(get_current_user)):
    card = _maybe(sb.table("cards").select("*").eq("card_id", card_id).maybe_single().execute())
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    if card["owner_id"] == user["user_id"]:
        raise HTTPException(status_code=400, detail="Cannot vote on your own card")

    expires_dt = _parse_dt(card["expires_at"])
    if expires_dt < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Card has expired")

    sb.table("votes").insert({
        "vote_id": f"vote_{uuid.uuid4().hex[:12]}",
        "voter_id": user["user_id"],
        "card_id": card_id,
        "owner_id": card["owner_id"],
        "vote_type": "ads",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    sb.table("cards").update({"votes": card.get("votes", 0) + 1}).eq("card_id", card_id).execute()

    new_progress = user.get("votes_since_token", 0) + 1
    tokens_earned = 0
    if new_progress >= VOTES_PER_TOKEN:
        tokens_earned = new_progress // VOTES_PER_TOKEN
        new_progress = new_progress % VOTES_PER_TOKEN
    new_tokens = user.get("tokens", 0) + tokens_earned
    sb.table("users").update({"votes_since_token": new_progress, "tokens": new_tokens}).eq("user_id", user["user_id"]).execute()

    return {
        "ok": True,
        "smart_link": card["smart_link"],
        "tokens": new_tokens,
        "votes_since_token": new_progress,
        "tokens_earned": tokens_earned,
    }


# ========= Image Library =========
SYSTEM_IMAGES = [
    "https://images.unsplash.com/photo-1723283126758-28f2a308bc47?crop=entropy&cs=srgb&fm=jpg&w=800&q=80",
    "https://images.unsplash.com/photo-1689154345830-861f74006b09?crop=entropy&cs=srgb&fm=jpg&w=800&q=80",
    "https://images.pexels.com/photos/29888428/pexels-photo-29888428.jpeg?auto=compress&cs=tinysrgb&w=800",
    "https://images.pexels.com/photos/25626583/pexels-photo-25626583.jpeg?auto=compress&cs=tinysrgb&w=800",
    "https://images.unsplash.com/photo-1639817754460-9af351966008?crop=entropy&cs=srgb&fm=jpg&w=800&q=80",
    "https://images.unsplash.com/photo-1557672172-298e090bd0f1?auto=format&fit=crop&w=800&q=80",
    "https://images.unsplash.com/photo-1558865869-c93f6f8482af?auto=format&fit=crop&w=800&q=80",
    "https://images.unsplash.com/photo-1579547945413-497e1b99dac0?auto=format&fit=crop&w=800&q=80",
    "https://images.unsplash.com/photo-1618331835717-801e976710b2?auto=format&fit=crop&w=800&q=80",
    "https://images.unsplash.com/photo-1550684848-fac1c5b4e853?auto=format&fit=crop&w=800&q=80",
    "https://images.unsplash.com/photo-1604871000636-074fa5117945?auto=format&fit=crop&w=800&q=80",
    "https://images.unsplash.com/photo-1614850523459-c2f4c699c52e?auto=format&fit=crop&w=800&q=80",
]

@api_router.get("/images/library")
def image_library(user: dict = Depends(get_current_user)):
    return {"images": SYSTEM_IMAGES}


def _get_built_in_cards(count: int) -> list:
    sponsor_cards = [
        {
            "card_id": f"sponsor_{uuid.uuid4().hex[:8]}",
            "owner_id": "stokvel_sponsor",
            "owner_name": "Stokvel 🌟",
            "image_url": SYSTEM_IMAGES[i % len(SYSTEM_IMAGES)],
            "smart_link": "https://www.profitablecpmratenetwork.com/z0eydp85?key=eaa584ff9abd40f5a68179eb17df1f1f",
            "title": f"Featured #{i+1}",
            "votes": random.randint(100, 1000),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
            "is_premium": True,
            "diamond_boosted": False,
            "owner_stellar_wallet": STOKVEL_TREASURY_PUBLIC,
            "vote_cost_xlm": VOTE_COST_XLM,
            "time_extensions": 0,
            "total_time_purchased": 0,
        }
        for i in range(count)
    ]
    return sponsor_cards


# ========= Stellar Routes =========

@api_router.get("/stellar/status")
def stellar_status(user: dict = Depends(get_current_user)):
    xlm_balance = 0.0
    if user.get("stellar_public_key"):
        xlm_balance = _check_stellar_balance(user["stellar_public_key"])
    
    return {
        "network": STELLAR_NETWORK,
        "upgrade_cost_xlm": UPGRADE_COST_XLM,
        "vote_cost_xlm": VOTE_COST_XLM,
        "user_public_key": user.get("stellar_public_key"),
        "user_seed": user.get("stellar_seed"),
        "membership_type": user.get("membership_type", "free"),
        "treasury_public": STOKVEL_TREASURY_PUBLIC,
        "xlm_balance": xlm_balance,
        "free_cards_used": _get_free_cards_count(user["user_id"]),
        "free_cards_limit": FREE_CARDS_PER_USER,
    }


@api_router.post("/stellar/upgrade")
def stellar_upgrade(payload: StellarUpgradePayload, user: dict = Depends(get_current_user)):
    if user.get("has_paid_upgrade"):
        raise HTTPException(status_code=400, detail="Already upgraded")
    
    try:
        transaction = STELLAR_SERVER.transactions().transaction(payload.transaction_hash).call()
        
        payment_found = False
        for operation in transaction.get('operations', []):
            if operation['type'] == 'payment':
                if (operation['to'] == STOKVEL_TREASURY_PUBLIC and 
                    operation['from'] == user.get('stellar_public_key') and
                    float(operation['amount']) >= UPGRADE_COST_XLM):
                    payment_found = True
                    break
        
        if not payment_found:
            raise HTTPException(status_code=400, detail="Payment not verified")
        
        sb.table("users").update({
            "membership_type": "crypto",
            "has_paid_upgrade": True,
            "upgrade_payment_id": payload.transaction_hash,
        }).eq("user_id", user["user_id"]).execute()
        
        sb.table("stellar_payments").insert({
            "payment_id": f"sp_{uuid.uuid4().hex[:12]}",
            "user_id": user["user_id"],
            "transaction_hash": payload.transaction_hash,
            "amount_xlm": UPGRADE_COST_XLM,
            "payment_type": "upgrade",
            "status": "complete",
        }).execute()
        
        return {
            "ok": True,
            "membership_type": "crypto",
            "message": "Successfully upgraded to Stellar membership"
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Payment verification failed: {str(e)}")


@api_router.post("/stellar/vote/{card_id}")
def stellar_vote(card_id: str, payload: StellarVotePayload, user: dict = Depends(get_current_user)):
    if user.get("membership_type") != "crypto":
        raise HTTPException(status_code=400, detail="Must be a crypto member to vote with XLM")
    
    card = _maybe(sb.table("cards").select("*").eq("card_id", card_id).maybe_single().execute())
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    if card["owner_id"] == user["user_id"]:
        raise HTTPException(status_code=400, detail="Cannot vote on your own card")
    
    expires_dt = _parse_dt(card["expires_at"])
    if expires_dt < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Card has expired")
    
    balance = _check_stellar_balance(user["stellar_public_key"])
    if balance < VOTE_COST_XLM:
        raise HTTPException(status_code=400, detail=f"Insufficient XLM balance. Need {VOTE_COST_XLM} XLM, have {balance:.2f} XLM")
    
    try:
        transaction = STELLAR_SERVER.transactions().transaction(payload.transaction_hash).call()
        
        card_owner = _maybe(sb.table("users").select("*").eq("user_id", card["owner_id"]).maybe_single().execute())
        if card_owner and card_owner.get("stellar_public_key"):
            sb.table("votes").insert({
                "vote_id": f"vote_{uuid.uuid4().hex[:12]}",
                "voter_id": user["user_id"],
                "card_id": card_id,
                "owner_id": card["owner_id"],
                "vote_type": "xlm",
                "xlm_amount": VOTE_COST_XLM,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
            
            sb.table("cards").update({"votes": card.get("votes", 0) + 1}).eq("card_id", card_id).execute()
            
            new_tokens = user.get("tokens", 0) + TOKENS_PER_VOTE
            sb.table("users").update({"tokens": new_tokens}).eq("user_id", user["user_id"]).execute()
            
            sb.table("stellar_payments").insert({
                "payment_id": f"sp_{uuid.uuid4().hex[:12]}",
                "user_id": user["user_id"],
                "transaction_hash": payload.transaction_hash,
                "amount_xlm": VOTE_COST_XLM,
                "payment_type": "vote",
                "status": "complete",
            }).execute()
            
            return {
                "ok": True,
                "smart_link": card["smart_link"],
                "tokens": new_tokens,
                "tokens_earned": TOKENS_PER_VOTE,
                "message": f"Vote counted! +{TOKENS_PER_VOTE} token"
            }
        else:
            raise HTTPException(status_code=400, detail="Card owner has no Stellar wallet")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Payment verification failed: {str(e)}")


# ========= Time Marketplace =========

@api_router.post("/marketplace/time/list")
def create_time_listing(payload: TimeListingCreate, user: dict = Depends(get_current_user)):
    if user.get("membership_type") != "crypto":
        raise HTTPException(status_code=400, detail="Must be a crypto member to sell time")
    
    if payload.minutes_available < TIME_PURCHASE_MIN_MINUTES:
        raise HTTPException(status_code=400, detail=f"Minimum {TIME_PURCHASE_MIN_MINUTES} minutes required")
    
    if payload.price_per_minute_xlm <= 0:
        raise HTTPException(status_code=400, detail="Price must be greater than 0")
    
    total_price = payload.minutes_available * payload.price_per_minute_xlm
    
    listing = {
        "listing_id": f"tlist_{uuid.uuid4().hex[:12]}",
        "seller_id": user["user_id"],
        "minutes_available": payload.minutes_available,
        "price_per_minute_xlm": payload.price_per_minute_xlm,
        "total_price_xlm": total_price,
        "is_active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    
    sb.table("time_marketplace").insert(listing).execute()
    
    return {"ok": True, "listing": listing}


@api_router.get("/marketplace/time/listings")
def get_time_listings(user: dict = Depends(get_current_user)):
    res = sb.table("time_marketplace").select("*").eq("is_active", True).neq("seller_id", user["user_id"]).execute()
    listings = res.data or []
    
    enriched = []
    for listing in listings:
        seller = _maybe(sb.table("users").select("name,stellar_public_key").eq("user_id", listing["seller_id"]).maybe_single().execute())
        listing["seller_name"] = seller.get("name", "Unknown") if seller else "Unknown"
        listing["seller_wallet"] = seller.get("stellar_public_key") if seller else None
        enriched.append(listing)
    
    return {"listings": enriched}


@api_router.post("/marketplace/time/buy")
def buy_time(payload: TimePurchasePayload, user: dict = Depends(get_current_user)):
    if user.get("membership_type") != "crypto":
        raise HTTPException(status_code=400, detail="Must be a crypto member to buy time")
    
    listing = _maybe(sb.table("time_marketplace").select("*").eq("listing_id", payload.listing_id).maybe_single().execute())
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    if not listing["is_active"]:
        raise HTTPException(status_code=400, detail="Listing is no longer active")
    if payload.minutes_to_buy > listing["minutes_available"]:
        raise HTTPException(status_code=400, detail="Not enough minutes available")
    
    card = _maybe(sb.table("cards").select("*").eq("card_id", payload.card_id).maybe_single().execute())
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    if card["owner_id"] != user["user_id"]:
        raise HTTPException(status_code=400, detail="You don't own this card")
    
    total_price = payload.minutes_to_buy * listing["price_per_minute_xlm"]
    
    try:
        transaction = STELLAR_SERVER.transactions().transaction(payload.transaction_hash).call()
        
        seller = _maybe(sb.table("users").select("stellar_public_key").eq("user_id", listing["seller_id"]).maybe_single().execute())
        if not seller:
            raise HTTPException(status_code=400, detail="Seller not found")
        
        payment_found = False
        for operation in transaction.get('operations', []):
            if operation['type'] == 'payment':
                if (operation['to'] == seller["stellar_public_key"] and 
                    operation['from'] == user.get('stellar_public_key') and
                    float(operation['amount']) >= total_price):
                    payment_found = True
                    break
        
        if not payment_found:
            raise HTTPException(status_code=400, detail="Payment not verified")
        
        current_expires = _parse_dt(card["expires_at"])
        new_expires = current_expires + timedelta(minutes=payload.minutes_to_buy)
        
        sb.table("cards").update({
            "expires_at": new_expires.isoformat(),
            "time_extensions": card.get("time_extensions", 0) + 1,
            "total_time_purchased": card.get("total_time_purchased", 0) + payload.minutes_to_buy,
        }).eq("card_id", payload.card_id).execute()
        
        remaining_minutes = listing["minutes_available"] - payload.minutes_to_buy
        if remaining_minutes <= 0:
            sb.table("time_marketplace").update({"is_active": False, "minutes_available": 0}).eq("listing_id", payload.listing_id).execute()
        else:
            sb.table("time_marketplace").update({"minutes_available": remaining_minutes}).eq("listing_id", payload.listing_id).execute()
        
        sb.table("time_purchases").insert({
            "purchase_id": f"tp_{uuid.uuid4().hex[:12]}",
            "buyer_id": user["user_id"],
            "seller_id": listing["seller_id"],
            "listing_id": payload.listing_id,
            "minutes_purchased": payload.minutes_to_buy,
            "price_xlm": total_price,
            "transaction_hash": payload.transaction_hash,
            "status": "complete",
        }).execute()
        
        return {
            "ok": True,
            "minutes_added": payload.minutes_to_buy,
            "new_expires_at": new_expires.isoformat(),
            "cost_xlm": total_price,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Payment verification failed: {str(e)}")


# ========= PayFast =========
def _payfast_signature(params: dict, passphrase: str = "") -> str:
    filtered = {k: v for k, v in params.items() if v not in (None, "")}
    pairs = []
    for k in sorted(filtered.keys()):
        v = str(filtered[k]).strip()
        pairs.append(f"{k}={quote(v, safe='')}")
    query = "&".join(pairs)
    if passphrase:
        query += f"&passphrase={quote(passphrase, safe='')}"
    return hashlib.md5(query.encode("utf-8")).hexdigest()


@api_router.post("/payments/payfast/initiate")
def payfast_initiate(payload: PayfastInitiatePayload, user: dict = Depends(get_current_user)):
    merchant_id = os.environ.get("PAYFAST_MERCHANT_ID", "")
    merchant_key = os.environ.get("PAYFAST_MERCHANT_KEY", "")
    passphrase = os.environ.get("PAYFAST_PASSPHRASE", "")
    sandbox = os.environ.get("PAYFAST_SANDBOX", "true").lower() == "true"

    m_payment_id = f"stokvel_{user['user_id']}_{uuid.uuid4().hex[:8]}"
    params = {
        "merchant_id": merchant_id,
        "merchant_key": merchant_key,
        "return_url": payload.return_url,
        "cancel_url": payload.cancel_url,
        "m_payment_id": m_payment_id,
        "amount": "5.00",
        "item_name": "Stokvel Premium (Monthly)",
        "email_address": user["email"],
        "subscription_type": "1",
        "billing_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "recurring_amount": "5.00",
        "frequency": "3",
        "cycles": "0",
    }
    signature = _payfast_signature(params, passphrase)
    params["signature"] = signature

    sb.table("subscriptions").insert({
        "m_payment_id": m_payment_id,
        "user_id": user["user_id"],
        "kind": "subscription",
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    base = "https://sandbox.payfast.co.za/eng/process" if sandbox else "https://www.payfast.co.za/eng/process"
    query = "&".join([f"{k}={quote(str(v), safe='')}" for k, v in params.items()])
    return {"redirect_url": f"{base}?{query}", "m_payment_id": m_payment_id}


@api_router.post("/payments/payfast/activate-sandbox")
def payfast_activate_sandbox(user: dict = Depends(get_current_user)):
    sandbox = os.environ.get("PAYFAST_SANDBOX", "true").lower() == "true"
    if not sandbox:
        raise HTTPException(status_code=400, detail="Only available in sandbox mode")
    premium_until = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    sb.table("users").update({"is_premium": True, "premium_until": premium_until}).eq("user_id", user["user_id"]).execute()
    return {"ok": True, "is_premium": True, "premium_until": premium_until}


# ========= App wiring =========
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://stokvel-cafbf.firebaseapp.com",
        "https://stokvel-cafbf.web.app",
        "http://localhost:3000",
        "http://localhost:8000"
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@app.on_event("startup")
def backfill_user_fields():
    try:
        res = sb.table("users").select("user_id").is_("referral_code", "null").execute()
        for u in (res.data or []):
            sb.table("users").update({"referral_code": uuid.uuid4().hex[:8]}).eq("user_id", u["user_id"]).execute()
    except Exception as e:
        logger.warning("Backfill skipped: %s", e)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)