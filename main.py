import asyncio
import time
import os
import json
from datetime import datetime, timezone, timedelta
import pytz
from urllib.parse import urlparse
from dotenv import load_dotenv
from web3 import Web3
from curl_cffi.requests import AsyncSession

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# --- CONFIGURATION ---
load_dotenv()

TOKEN = os.environ.get("BOT_TOKEN")
RPC_URL = os.environ.get("RPC_URL", "https://morning-small-arrow.ethereum-mainnet.quiknode.pro/97cdbfc4b18057687be47f87a23b7a53c702c8c6/")
OPENSEA_API_KEY = os.environ.get("OPENSEA_API_KEY")
DEFAULT_JWT = os.environ.get("DEFAULT_JWT")

if not TOKEN or not DEFAULT_JWT:
    print("❌ ERROR: BOT_TOKEN or DEFAULT_JWT is missing in environment variables!")
    exit(1)

ABI = [
    {"inputs": [], "name": "totalSupply", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "maxSupply", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}
]

w3 = Web3(Web3.HTTPProvider(RPC_URL))
user_data = {}
tz_utc = pytz.timezone('UTC')
tz_bdt = pytz.timezone('Asia/Dhaka')
active_tasks = {}
ADMIN_ID = 1890133465

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ <b>Unauthorized access. You are not the admin.</b>", parse_mode=ParseMode.HTML)
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = """👋 <b>Welcome to Alpha Mint Tracker Bot!</b>

This bot will live monitor your provided OpenSea projects and alert you the exact moment minting starts.

🤖 <b>Features:</b>
✅ Eligibility Check via OpenSea JWT
✅ Live Phase Tracking from OpenSea API
✅ Live Mint Count & Sold Out Alerts
✅ Price in ETH + USD + BDT/UTC Time

🛠️ <b>Getting Started:</b>
Type /help to see all available commands!"""
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

@admin_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = """🛠️ <b>Available Commands:</b>

🔹 <b>/setwallet [address]</b> - Set your wallet address
🔹 <b>/mywallet</b> - View your currently saved wallet
🔹 <b>/add [link]</b> - Check eligibility for a drop (No live tracking)
🔹 <b>/track [link]</b> - Check eligibility AND start live tracking
🔹 <b>/list</b> - View all actively tracked projects
🔹 <b>/remove [slug]</b> - Stop tracking a specific project
🔹 <b>/removeall</b> - Stop tracking ALL projects
🔹 <b>/help</b> - Show this help menu"""
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


@admin_only
async def set_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Example: /setwallet 0x...", parse_mode=ParseMode.HTML)
        return
    addr = context.args[0]
    if w3.is_address(addr):
        wallet = w3.to_checksum_address(addr)
        user_id = update.effective_user.id
        if user_id not in user_data: user_data[user_id] = {}
        user_data[user_id]["wallet"] = wallet
        await update.message.reply_text(f"✅ <b>Wallet saved!</b>\n<code>{wallet}</code>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Invalid Ethereum address.")

@admin_only
async def my_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    wallet = user_data.get(user_id, {}).get("wallet")
    if wallet:
        await update.message.reply_text(f"💳 <b>Your Saved Wallet:</b>\n<code>{wallet}</code>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("⚠️ <b>No wallet saved.</b> Use /setwallet 0x...", parse_mode=ParseMode.HTML)

@admin_only
async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not active_tasks:
        await update.message.reply_text("📉 <b>No projects are currently being tracked.</b>", parse_mode=ParseMode.HTML)
        return
    msg = "📋 <b>Currently Tracking:</b>\n\n"
    for slug, info in active_tasks.items():
        start_info = f"   Starts: {info['start_bdt']}\n" if info.get('start_bdt') and info['start_bdt'] != 'Not Set' else "   Starts: Not Set\n"
        msg += f"🔹 <b>{slug}</b>\n   Phase: {info['phase']}\n{start_info}   Monitoring Since: {info['monitoring_since']}\n\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def fetch_drop_data(slug: str, wallet_address: str):
    url = "https://gql.opensea.io/graphql"
    
    # Send both access_token and connected-account-server-hint which OpenSea requires
    cookie_str = f"access_token={DEFAULT_JWT}; connected-account-server-hint={wallet_address.lower()}"
    
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "Origin": "https://opensea.io",
        "Referer": f"https://opensea.io/collection/{slug}",
        "x-app-id": "os2-web",
        "x-graphql-operation-type": "query",
        "Cookie": cookie_str,
    }

    payload = {
        "operationName": "DropEligibilityQuery",
        "variables": {"address": wallet_address, "collectionSlug": slug},
        "extensions": {
            "persistedQuery": {
                "sha256Hash": "d893f026d731e8f14986921fa4229098e018289f6cc7683f8ee2dd83749dd95d",
                "version": 1
            }
        }
    }
    for attempt in range(3):
        try:
            async with AsyncSession(impersonate="chrome110") as s:
                response = await s.post(url, headers=headers, json=payload, timeout=15)
                if response.status_code == 200:
                    data = response.json()
                    return data
        except Exception as e:
            pass
        await asyncio.sleep(2)
    return None


def format_timestamp(ts):
    if not ts:
        return "Not Set", "Not Set"
    try:
        dt = datetime.fromtimestamp(ts, tz_utc)
        utc_str = dt.strftime('%d %b %Y, %I:%M %p UTC')
        bdt_str = dt.astimezone(tz_bdt).strftime('%d %b %Y, %I:%M %p BDT')
        return utc_str, bdt_str
    except:
        return "Unknown", "Unknown"


def format_bdt(ts_str):
    if not ts_str:
        return "Not Set"
    try:
        # Handle formats like 2026-07-13T12:55:26.000Z or 2026-07-13T12:55:26Z
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts_str)
        bdt = dt.astimezone(timezone(timedelta(hours=6)))
        return bdt.strftime("%b %d at %I:%M %p GMT+6")
    except Exception as e:
        return "Unknown"

async def fetch_html_drop_data(slug: str):
    url = f"https://opensea.io/collection/{slug}/drop"
    for attempt in range(3):
        try:
            async with AsyncSession(impersonate="chrome110") as s:
                resp = await s.get(url, timeout=15)
                if resp.status_code == 200:
                    html = resp.text
                    clean_html = html.replace('\\"', '"').replace('\\\\', '\\')
                    idx = clean_html.find('"dropBySlug":{"__typename"')
                    if idx != -1:
                        start_idx = idx + len('"dropBySlug":')
                        count = 0
                        end_idx = -1
                        for i in range(start_idx, len(clean_html)):
                            if clean_html[i] == '{':
                                count += 1
                            elif clean_html[i] == '}':
                                count -= 1
                                if count == 0:
                                    end_idx = i + 1
                                    break
                        if end_idx != -1:
                            json_str = clean_html[start_idx:end_idx]
                            return json.loads(json_str)
        except Exception as e:
            pass
        await asyncio.sleep(2)
    return None

async def process_project(update: Update, context: ContextTypes.DEFAULT_TYPE, is_tracking: bool):
    try:
        if not context.args:
            await update.message.reply_text("❌ <b>Please provide a link.</b>\nFormat: /track [link] [optional_contract_address]", parse_mode=ParseMode.HTML)
            return

        link = context.args[0]
        user_id = update.effective_user.id
        u_data = user_data.get(user_id, {})
        wallet = u_data.get("wallet")

        if not wallet:
            await update.message.reply_text("⚠️ <b>Set your wallet first:</b> /setwallet 0x...", parse_mode=ParseMode.HTML)
            return

        slug = "Unknown"
        parsed = urlparse(link)
        path_parts = [p for p in parsed.path.split("/") if p]
        if "collection" in path_parts:
            idx = path_parts.index("collection")
            if idx + 1 < len(path_parts):
                slug = path_parts[idx + 1]
        elif path_parts:
            slug = path_parts[-1]

        if slug == "Unknown":
            await update.message.reply_text("❌ <b>Could not extract slug from link.</b>", parse_mode=ParseMode.HTML)
            return

        status_msg = await update.message.reply_text("🔍 <b>Fetching Drop Data & Eligibility...</b>", parse_mode=ParseMode.HTML)

        # Allow user to manually pass contract address to bypass API Key restriction
        contract_address = None
        if len(context.args) > 1 and w3.is_address(context.args[1]):
            contract_address = w3.to_checksum_address(context.args[1])
        
        # If not provided, try the API
        if not contract_address:
            try:
                os_api = f"https://api.opensea.io/api/v2/collections/{slug}"
                headers = {"X-API-KEY": OPENSEA_API_KEY}
                async with AsyncSession(impersonate="chrome110") as s:
                    os_res = await s.get(os_api, headers=headers, timeout=10)
                    if os_res.status_code == 200:
                        os_data = os_res.json()
                        if 'contracts' in os_data and os_data['contracts']:
                            contract_address = w3.to_checksum_address(os_data['contracts'][0]['address'])
            except:
                pass

        contract = None
        if contract_address:
            try:
                contract = w3.eth.contract(address=contract_address, abi=ABI)
            except:
                pass

        # Run both fetches concurrently
        html_task = asyncio.create_task(fetch_html_drop_data(slug))
        gql_task = asyncio.create_task(fetch_drop_data(slug, wallet))
        html_data, gql_data = await asyncio.gather(html_task, gql_task)

        supply_text = "Supply: Unknown"
        max_s_html = html_data.get('maxSupply') if html_data else None
        curr_s_html = html_data.get('totalSupply') if html_data else None

        if contract:
            try:
                max_s = contract.functions.maxSupply().call()
                curr_s = contract.functions.totalSupply().call()
                supply_text = f"Supply: {curr_s} / {max_s}"
            except:
                if max_s_html is not None and curr_s_html is not None:
                    supply_text = f"Supply: {curr_s_html} / {max_s_html}"
        elif max_s_html is not None and curr_s_html is not None:
            supply_text = f"Supply: {curr_s_html} / {max_s_html}"

        if not gql_data or 'data' not in gql_data or not gql_data['data'].get('dropBySlug'):
            await status_msg.edit_text("❌ <b>Not a valid OpenSea Drop or drop not found.</b>", parse_mode=ParseMode.HTML)
            return

        gql_stages = gql_data['data']['dropBySlug'].get('stages', [])
        html_stages = html_data.get('stages', []) if html_data else []
        
        # Merge by stageIndex
        html_stage_map = {s.get('stageIndex'): s for s in html_stages if 'stageIndex' in s}
        
        if not gql_stages:
            await status_msg.edit_text("❌ <b>No mint stages found.</b>", parse_mode=ParseMode.HTML)
            return

        all_phases_text = ""
        target_stage = None

        for g_stage in gql_stages:
            idx = g_stage.get('stageIndex')
            h_stage = html_stage_map.get(idx, {})
            
            # Label
            label = h_stage.get('label') or g_stage.get('stageType', 'Unknown Phase')
            
            # Start/End
            start_ts = h_stage.get('startTime') or g_stage.get('startTime')
            end_ts = h_stage.get('endTime') or g_stage.get('endTime')
            
            start_bdt = format_bdt(start_ts)
            end_bdt = format_bdt(end_ts)
            
            # Price
            token_price = (h_stage.get('price') or g_stage.get('eligiblePrice') or {}).get('token', {}).get('unit', 0)
            usd_price = (h_stage.get('price') or g_stage.get('eligiblePrice') or {}).get('usd', 0)
            
            if float(token_price) == 0:
                price_str = "FREE"
            else:
                if usd_price and float(usd_price) > 0:
                    price_str = f"{token_price} ETH (${round(float(usd_price), 2)})"
                else:
                    price_str = f"{token_price} ETH"
                
            # Limit
            limit = h_stage.get('maxTotalMintableByWallet') or g_stage.get('maxTotalMintableByWallet')
            limit_str = f"LIMIT {limit} PER WALLET" if limit else "NO LIMIT"
            
            # Eligibility
            s_eligible = g_stage.get('isEligible')
            if s_eligible is True:
                el_text = "✅ ELIGIBLE"
                if not target_stage:
                    target_stage = g_stage
            elif s_eligible is False:
                el_text = "❌ NOT ELIGIBLE"
            else:
                el_text = "⚠️ UNKNOWN"

            all_phases_text += f"\n🔹 <b>{label}</b>\n"
            all_phases_text += f"Starts: {start_bdt}\n"
            if end_bdt != "Not Set":
                all_phases_text += f"Ends: {end_bdt}\n"
            all_phases_text += f"{price_str} | {limit_str}\n"
            all_phases_text += f"Status: {el_text}\n"

        if not target_stage:
            target_stage = gql_stages[-1]

        wallet_short = f"{wallet[:6]}...{wallet[-4:]}"
        
        msg = f"""🖼️ <b>Project: {slug}</b> (https://opensea.io/collection/{slug}/drop)
💳 <b>Wallet:</b> {wallet_short}
📊 <b>{supply_text}</b>

🗓 <b>MINT SCHEDULE:</b>
{all_phases_text}"""

        if is_tracking:
            msg += "\n👀 <b>Monitoring...</b>"

        await status_msg.edit_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

        if not is_tracking:
            return

        if not contract_address or not contract:
            await context.bot.send_message(update.effective_chat.id, "⚠️ <b>Contract Address not found. Live minting won't be tracked.</b>", parse_mode=ParseMode.HTML)
            return

        upcoming_phases = []
        for g_stage in gql_stages:
            idx = g_stage.get('stageIndex')
            h_stage = html_stage_map.get(idx, {})
            start_time = h_stage.get('startTime') or g_stage.get('startTime')
            end_time = h_stage.get('endTime') or g_stage.get('endTime')
            
            start_ts = None
            if start_time:
                try:
                    st_iso = start_time[:-1] + "+00:00" if start_time.endswith("Z") else start_time
                    start_ts = datetime.fromisoformat(st_iso).timestamp()
                except:
                    pass
            
            # Skip if already ended
            if end_time:
                try:
                    et_iso = end_time[:-1] + "+00:00" if end_time.endswith("Z") else end_time
                    end_ts = datetime.fromisoformat(et_iso).timestamp()
                    if end_ts < time.time():
                        continue
                except:
                    pass
            
            stage_name = h_stage.get('label') or g_stage.get('stageType', 'Unknown Phase')
            is_eligible = g_stage.get('isEligible')
            
            upcoming_phases.append({
                "stage_name": stage_name,
                "start_time": start_time,
                "start_ts": start_ts,
                "is_eligible": is_eligible
            })
            
        if not upcoming_phases:
            await context.bot.send_message(update.effective_chat.id, "⚠️ <b>No upcoming phases found to track.</b>", parse_mode=ParseMode.HTML)
            return

        task = asyncio.create_task(run_monitor(update, context, contract, slug, upcoming_phases))
        task.add_done_callback(lambda t: None)

    except Exception as e:
        await update.message.reply_text(f"❌ <b>Error:</b> {e}", parse_mode=ParseMode.HTML)

@admin_only
async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_project(update, context, is_tracking=True)

@admin_only
async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await process_project(update, context, is_tracking=False)

@admin_only
async def remove_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ <b>Format: /remove slug</b>", parse_mode=ParseMode.HTML)
        return
    slug = context.args[0]
    if slug in active_tasks:
        active_tasks.pop(slug, None)
        await update.message.reply_text(f"✅ <b>Removed {slug} from tracking.</b>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"⚠️ <b>{slug} is not being tracked.</b>", parse_mode=ParseMode.HTML)

@admin_only
async def remove_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active_tasks.clear()
    await update.message.reply_text("✅ <b>Removed ALL tracked projects.</b>", parse_mode=ParseMode.HTML)

async def run_monitor(update, context, contract, slug, upcoming_phases):
    chat_id = update.effective_chat.id

    first_st = upcoming_phases[0]
    active_tasks[slug] = {
        "phase": first_st["stage_name"],
        "start_bdt": format_bdt(first_st["start_time"]),
        "monitoring_since": datetime.now(timezone(timedelta(hours=6))).strftime("%b %d at %I:%M %p GMT+6")
    }

    async def poll_supply():
        if contract is None:
            while slug in active_tasks:
                await asyncio.sleep(60)
            return

        try:
            max_s = contract.functions.maxSupply().call()
        except:
            max_s = None

        if not max_s:
            while slug in active_tasks:
                await asyncio.sleep(60)
            return

        while slug in active_tasks:
            try:
                curr = contract.functions.totalSupply().call()
                if curr >= max_s:
                    if slug in active_tasks:
                        active_tasks.pop(slug, None)
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"🔴 <b>SOLD OUT!</b> {slug} has minted out.",
                        parse_mode=ParseMode.HTML
                    )
                    break
            except:
                pass
            await asyncio.sleep(10)

    async def poll_phases():
        for i, st in enumerate(upcoming_phases):
            if slug not in active_tasks:
                break

            phase_name = st["stage_name"]
            start_ts = st["start_ts"]
            is_el = st["is_eligible"]
            
            # Update active_tasks for the current phase we're waiting for
            if slug in active_tasks:
                active_tasks[slug]["phase"] = phase_name
                active_tasks[slug]["start_bdt"] = format_bdt(st["start_time"])

            if is_el is True:
                el_text = "✅ YOU ARE ELIGIBLE!"
            elif is_el is False:
                el_text = "❌ You are NOT ELIGIBLE."
            else:
                el_text = "⚠️ Eligibility Unknown."

            if start_ts:
                now = time.time()
                wait_seconds = start_ts - now

                if wait_seconds <= 0:
                    # Only send "ALREADY LIVE" for the very first phase we process if it's past due
                    if i == 0:
                        if slug in active_tasks:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=f"🟡 <b>PHASE IS ALREADY LIVE!</b>\n⚠️ Mint is currently in progress.\n\n🖼 Project: <b>{slug}</b>\n🔹 Phase: <b>{phase_name}</b>\n{el_text}\n\nGo to OpenSea (https://opensea.io/collection/{slug}/drop)",
                                parse_mode=ParseMode.HTML, disable_web_page_preview=True
                            )
                else:
                    if wait_seconds > 10:
                        wait_msg = await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"⏳ <b>Waiting for phase to start...</b>\nTime left: {int(wait_seconds)} seconds",
                            parse_mode=ParseMode.HTML
                        )
                        chunk = 5
                        slept = 0
                        target_sleep = wait_seconds - 5
                        while slept < target_sleep and slug in active_tasks:
                            sleep_for = min(chunk, target_sleep - slept)
                            await asyncio.sleep(sleep_for)
                            slept += sleep_for

                        if slug in active_tasks:
                            await wait_msg.edit_text("🔔 <b>Phase starting in 5 seconds...</b>", parse_mode=ParseMode.HTML)
                            # Wait final 5 seconds checking if active
                            for _ in range(5):
                                if slug not in active_tasks:
                                    break
                                await asyncio.sleep(1)
                    else:
                        await asyncio.sleep(wait_seconds)

                    if slug in active_tasks:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"🔔 <b>PHASE IS LIVE!</b>\n\n🖼 Project: <b>{slug}</b>\n🔹 Phase: <b>{phase_name}</b>\n{el_text}\n\nGo to OpenSea (https://opensea.io/collection/{slug}/drop)",
                            parse_mode=ParseMode.HTML, disable_web_page_preview=True
                        )
            else:
                if slug in active_tasks:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"⏳ <b>PHASE IS TBA (Not Scheduled)</b>\nMonitoring supply just in case...\n\n🖼 Project: <b>{slug}</b>\n🔹 Phase: <b>{phase_name}</b>\n{el_text}\n\nGo to OpenSea (https://opensea.io/collection/{slug}/drop)",
                        parse_mode=ParseMode.HTML, disable_web_page_preview=True
                    )
                    
        # After all phases are done, if we can't monitor supply, we can remove the task
        if contract is None:
            active_tasks.pop(slug, None)

    await asyncio.gather(poll_supply(), poll_phases())

if __name__ == "__main__":
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("setwallet", set_wallet))
    app.add_handler(CommandHandler("mywallet", my_wallet))
    app.add_handler(CommandHandler("track", track))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("remove", remove_task))
    app.add_handler(CommandHandler("removeall", remove_all))
    app.add_handler(CommandHandler("list", list_tasks))

    print(f"Web3 Connected: {w3.is_connected()}")
    print("Bot is polling...")
    app.run_polling()
