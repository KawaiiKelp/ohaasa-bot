# --- 라이브러리 임포트 ---
import os
import json
import time
import asyncio
import logging
import datetime as dt
from typing import Dict, Any, Optional
import re  # <--- 이 줄 추가

import discord
from discord import app_commands
from dotenv import load_dotenv
import requests
import aiohttp
from bs4 import BeautifulSoup

from datetime import datetime, timezone, timedelta

# --- KST 유틸 ---
KST = timezone(timedelta(hours=9))

def now_kst() -> datetime:
    """항상 KST 기준 현재 시각 반환."""
    return datetime.now(timezone.utc).astimezone(KST)

def today_kst_yyyymmdd() -> str:
    """오늘 날짜를 KST 기준 YYYYMMDD로 반환."""
    return now_kst().strftime("%Y%m%d")

# --- 로깅 설정 ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# --- 환경 변수 로드 ---
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
TEST_GUILD_ID = os.getenv("DISCORD_TEST_GUILD_ID")

if not DISCORD_BOT_TOKEN:
    logging.error("오류: DISCORD_BOT_TOKEN이 .env에 설정되지 않았습니다.")
    raise SystemExit

MY_GUILD: Optional[discord.Object] = None
if TEST_GUILD_ID:
    try:
        MY_GUILD = discord.Object(id=int(TEST_GUILD_ID))
    except ValueError:
        logging.error("오류: DISCORD_TEST_GUILD_ID가 올바른 숫자 형식이 아닙니다.")
        raise SystemExit

# --- 상수 ---
OHAASA_URL = "https://www.asahi.co.jp/ohaasa/week/horoscope/"
OHAASA_JSON_URL = "https://www.asahi.co.jp/data/ohaasa2020/horoscope.json"
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/gemini-2.5-flash:generateContent"
)

GUILD_CONFIG_PATH = "guild_config.json"

# --- 디스코드 클라이언트 ---
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# guild_id(int) -> 설정 dict
guild_settings: Dict[int, Dict[str, Any]] = {}

# 길드별 오늘 운세 캐시: { guild_id: { "date": "YYYYMMDD", "data": [ ...translated... ] } }
# 기존: horoscope_cache: Dict[int, Dict[str, Any]] = {}
# 변경: 날짜별로 딱 하나만 저장
horoscope_cache: Dict[str, Any] = {}
cache_lock = asyncio.Lock()
fetch_lock = asyncio.Lock() # <--- 데이터 로딩 자체를 보호할 락 추가


# --- 길드 설정 로드/저장 ---

def load_guild_config() -> None:
    """guild_config.json에서 서버별 설정을 불러온다."""
    global guild_settings

    if not os.path.exists(GUILD_CONFIG_PATH):
        logging.info("guild_config.json이 없어 새로 생성 예정입니다.")
        guild_settings = {}
        return

    try:
        with open(GUILD_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logging.error(f"guild_config.json 로드 중 오류: {e}")
        guild_settings = {}
        return

    guild_settings = {int(gid): cfg for gid, cfg in raw.items()}
    logging.info(f"총 {len(guild_settings)}개의 길드 설정을 불러왔습니다.")


def save_guild_config() -> None:
    """현재 설정을 guild_config.json에 저장한다."""
    try:
        raw = {str(gid): cfg for gid, cfg in guild_settings.items()}
        with open(GUILD_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"guild_config.json 저장 중 오류: {e}")


def get_or_create_guild_settings(guild_id: int) -> Dict[str, Any]:
    """해당 길드의 설정이 없으면 기본값으로 생성하고 반환."""
    if guild_id not in guild_settings:
        guild_settings[guild_id] = {
            "channel_id": None,
            "post_hour": 8,          # 기본 자동 발사 시간: 08:00 (KST)
            "post_minute": 0,
            "gemini_api_key": "",
            "last_post_date": None,  # YYYYMMDD (KST)
            "mention_mode": "none",  # none / everyone / role
            "mention_role_id": None,
        }
    return guild_settings[guild_id]


def get_guild_settings(guild_id: int) -> Optional[Dict[str, Any]]:
    return guild_settings.get(guild_id)


# --- 권한 체크: 서버 소유자 전용 ---

def is_guild_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        return interaction.user.id == interaction.guild.owner_id
    return app_commands.check(predicate)


# --- Gemini 번역 함수 (재시도 포함) ---

async def translate_text(
    japanese_text: str,
    gemini_api_key: str,
    max_retries: int = 3,
) -> Optional[Any]:
    if not gemini_api_key:
        return None

    system_prompt = (
        "You are an expert translator specializing in Japanese-to-Korean horoscopes. "
        "Your goal is to translate Japanese horoscope content into natural, friendly Korean that maintains the original's energetic and positive tone. "
        "1. Keep exclamation marks (!), hearts, or other expressive punctuation if present in the original text. "
        "2. Translate into '해요체', ensuring the tone sounds like a fun, daily horoscope reading. "
        "3. IMPORTANT: The 'description_ko' field MUST ONLY contain the horoscope advice. "
        "4. DO NOT include any 'Lucky Color', 'Lucky Item', or 'Scores' in the 'description_ko' field. Extract ONLY the advice text. "
        "5. Extract exactly 12 rankings. "
        "6. Return ONLY the raw JSON array of 12 objects."
    )

    response_schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "rank": {"type": "INTEGER", "description": "Ranking (1 to 12)"},
                "sign_ko": {"type": "STRING", "description": "Korean zodiac sign"},
                "description_ko": {"type": "STRING", "description": "Horoscope text"}
            },
            "required": ["rank", "sign_ko", "description_ko"],
        },
    }

    payload = {
        "contents": [{"parts":[{"text": japanese_text}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        },
    }

    headers = {"Content-Type": "application/json"}

    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{GEMINI_API_URL}?key={gemini_api_key}", headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        json_string = result["candidates"][0]["content"]["parts"][0]["text"].strip()
                        if json_string.startswith("```"):
                            json_string = json_string.strip("`").replace("json", "", 1).strip()
                        return json.loads(json_string)

                    if 500 <= resp.status < 600:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(1 + attempt)
                            continue
                    return None
        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(1 + attempt)
                continue
            return None
    return None


# --- 오하아사 JSON 가져오기 ---

def fetch_horoscope_data_sync():
    """오하아사 JSON API를 로드합니다."""
    logging.info("오하아사 운세 데이터(JSON) 가져오기 시작")
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(OHAASA_JSON_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        
        data = resp.json()
        if not isinstance(data, list) or not data:
            return None

        root = data[0]
        onair_date = root.get("onair_date")
        if onair_date != today_kst_yyyymmdd():
            return None

        details = root.get("detail",[])
        
        # 이전처럼 파이썬에서 하나하나 텍스트를 자르지 않고, 
        # 원본 JSON을 그대로 줘서 Gemini가 숨겨진 럭키 아이템이나 색상도 알아서 찾게 만듭니다.
        return json.dumps(details, ensure_ascii=False)

    except Exception as e:
        logging.error(f"오하아사 JSON 로드 중 오류: {e}")
        return None

def fetch_gogo_data_sync() -> Optional[str]:
    """고고 별자리 파싱 (초고속 압축 최적화)"""
    logging.info("고고 별자리 데이터 가져오기 시작")
    url = "https://www.tv-asahi.co.jp/goodmorning/uranai/"
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding

        soup = BeautifulSoup(resp.text, "html.parser")
        
        # 불필요한 태그 완전 삭제
        for tag in soup(["script", "style", "header", "footer", "nav", "noscript", "svg", "img", "a"]):
            tag.decompose()
            
        # 정규식을 이용해 모든 공백과 줄바꿈을 하나의 띄어쓰기로 압축 (토큰 낭비 방지)
        text_content = re.sub(r'\s+', ' ', soup.get_text(strip=True))
        
        # 3000자만 넘겨도 핵심 운세 정보는 다 들어갑니다.
        return text_content[:3000]

    except Exception as e:
        logging.error(f"고고 별자리 파싱 오류: {e}")
        return None

async def get_today_horoscope_for_guild(
    guild_id: int,
    gemini_api_key: str,
) -> Optional[Dict[str, Any]]:
    today = today_kst_yyyymmdd()

    # 1단계: 이미 캐시가 있는지 확인
    async with cache_lock:
        if horoscope_cache.get(today):
            return horoscope_cache[today]

    # 2단계: 캐시가 없으면, 로딩 락을 획득하여 한 번만 로드하도록 함
    async with fetch_lock:
        # 락 획득 후 다시 한번 캐시 확인 (다른 길드가 이미 로드했을 수 있으므로)
        async with cache_lock:
            if horoscope_cache.get(today):
                return horoscope_cache[today]

        logging.info(f"===> 오늘자({today}) 운세 단독 로드 시작 (딱 한 번만 실행됨) <==")
        
        japanese_data = await asyncio.to_thread(fetch_horoscope_data_sync)
        if not japanese_data:
            logging.warning("오하아사 데이터 없음. 고고 별자리 전환.")
            japanese_data = await asyncio.to_thread(fetch_gogo_data_sync)
            
        translated_data = await translate_text(japanese_data, gemini_api_key) if japanese_data else None

        if translated_data:
            result = {
                "date": today,
                "source": "오하아사" if japanese_data else "고 고 별자리",
                "source_url": OHAASA_URL if japanese_data else "https://www.tv-asahi.co.jp/goodmorning/uranai/",
                "data": translated_data,
            }
            async with cache_lock:
                horoscope_cache[today] = result
            return result
            
        return None

    result = {
        "date": today,
        "source": source_name,
        "source_url": source_url,
        "data": translated_data,
    }

    # 3) 공통 캐시에 저장
    async with cache_lock:
        horoscope_cache[today] = result

    return result

# --- 디스코드 게시 로직 ---

async def fetch_and_post_horoscope(
    channel: discord.abc.Messageable,
    gemini_api_key: str,
    mention_text: Optional[str] = None,
    guild_id: Optional[int] = None,
) -> None:
    """
    오하아사 JSON을 받아 Gemini로 번역한 뒤
    지정된 채널에 운세를 게시한다.
    - JSON onair_date가 오늘이 아니면 '아직 갱신 안 됨' 메시지 출력
    """
    loading_content = "✨ **[오하아사 별자리 운세]** 데이터를 가져오는 중입니다..."
    if mention_text:
        loading_content = f"{mention_text} {loading_content}"

    loading_message = await channel.send(loading_content)

    # 길드 ID 추론
    if guild_id is None and hasattr(channel, "guild") and channel.guild:
        guild_id = channel.guild.id

    if guild_id is None:
        await loading_message.edit(
            content="❌ 길드 정보를 찾을 수 없어 운세를 불러오지 못했습니다."
        )
        return

    # 1+2. 캐시 포함 '오늘자 번역된 운세' 가져오기 (dict 반환)
    horoscope_info = await get_today_horoscope_for_guild(guild_id, gemini_api_key)

    if not horoscope_info:
        await loading_message.edit(
            content=(
                "❌ 오늘자 운세 데이터를 불러오지 못했습니다.\n"
                "오하아사/고고 별자리가 모두 갱신되지 않았거나 번역 오류가 발생했을 수 있습니다."
            )
        )
        return
        
    # 정보 추출
    source_name = horoscope_info["source"]
    source_url = horoscope_info["source_url"]
    translated_data = horoscope_info["data"]

    # 3. 디스코드 Embed + 스레드로 게시
    try:
        date_str = now_kst().strftime("%Y년 %m월 %d일")

        embed = discord.Embed(
            title=f"📅 {date_str} 오늘의 {source_name} 랭킹",
            description=f"[원문 출처: {source_name}](<{source_url}>)",
            color=0x4E72B7 if source_name == "오하아사" else 0xFF9900,
        )

        top_rankings = translated_data[:6]
        bottom_rankings = translated_data[6:]

        # 메인 Embed에는 깔끔하게 순위와 별자리만 표시 (rank가 숫자로 오기 때문에 '위'를 붙여줌)
        top_list = "\n".join(f"**{item['rank']}위** — {item['sign_ko']}" for item in top_rankings)
        bottom_list = "\n".join(f"**{item['rank']}위** — {item['sign_ko']}" for item in bottom_rankings)

        embed.add_field(name="🥇 상위 랭킹 (1~6위)", value=top_list or "데이터 없음", inline=True)
        embed.add_field(name="⬇️ 하위 랭킹 (7~12위)", value=bottom_list or "데이터 없음", inline=True)

        await loading_message.edit(content=None, embed=embed)
        initial_message = loading_message

        # 상세 내용 스레드 생성
        try:
            thread = await initial_message.create_thread(
                name=f"{date_str} 별자리 운세 상세",
                auto_archive_duration=60,
            )
        except Exception as e:
            thread = channel

        # 스레드에 예쁘게 포맷팅하여 올리는 헬퍼 함수
        # 헬퍼 함수 간소화
        def build_details_text(rankings, title):
            text = f"**{title}**\n"
            for item in rankings:
                text += f"\n**{item['rank']}위 {item['sign_ko']}**\n"
                text += f"> {item['description_ko']}\n"
            return text

        top_details_text = build_details_text(top_rankings, "🥇 상위 랭킹 상세")
        bottom_details_text = build_details_text(bottom_rankings, "⬇️ 하위 랭킹 상세")

        await thread.send(top_details_text)
        await thread.send(bottom_details_text)

        logging.info("운세 정보 게시 완료.")

    except Exception as e:
        logging.error(f"디스코드 메시지 게시 중 오류: {e}")
        await channel.send(f"❌ 운세 정보를 게시하는 중 오류가 발생했습니다: {e}")


# --- 자동 스케줄러 ---

async def scheduler_loop():
    """
    모든 길드 설정을 기준으로,
    매 분마다 현재 시간이 설정된 시각과 일치하면 자동으로 운세를 게시한다.
    시간 기준은 항상 KST(UTC+9)를 사용한다.
    """
    await client.wait_until_ready()
    logging.info("자동 운세 게시 스케줄러 시작 (KST 기준)")

    while not client.is_closed():
        now = now_kst()
        today_str = now.strftime("%Y%m%d")

        for guild_id, cfg in guild_settings.items():
            channel_id = cfg.get("channel_id")
            hour = cfg.get("post_hour")
            minute = cfg.get("post_minute")
            gemini_key = cfg.get("gemini_api_key")
            last_post_date = cfg.get("last_post_date")

            # 채널/키 미설정 시 스킵
            if not channel_id or not gemini_key:
                continue

            # 이미 오늘 올렸으면 스킵
            if last_post_date == today_str:
                continue

            # 설정한 시간과 현재 KST 시간이 일치할 때만 발사
            if now.hour == int(hour) and now.minute == int(minute):
                channel = client.get_channel(int(channel_id))
                if not channel:
                    logging.error(
                        f"길드 {guild_id}의 채널 ID {channel_id}를 찾을 수 없습니다."
                    )
                    continue

                # 멘션 텍스트 구성
                mention_text: Optional[str] = None
                mode = cfg.get("mention_mode", "none")
                role_id = cfg.get("mention_role_id")

                if mode == "everyone":
                    mention_text = "@everyone"
                elif mode == "role" and role_id:
                    mention_text = f"<@&{int(role_id)}>"

                logging.info(
                    f"길드 {guild_id}에 대해 자동 운세 게시 실행 (채널 {channel_id}, {hour:02d}:{minute:02d} KST)"
                )
                client.loop.create_task(
                    fetch_and_post_horoscope(channel, gemini_key, mention_text, guild_id)
                )

                cfg["last_post_date"] = today_str
                save_guild_config()

        await asyncio.sleep(30)


# --- 이벤트 ---

@client.event
async def on_ready():
    try:
        if MY_GUILD:
            tree.copy_global_to(guild=MY_GUILD)
            await tree.sync(guild=MY_GUILD)
        else:
            await tree.sync()

        logging.info(f"로그인 성공: {client.user} (ID: {client.user.id})")
        logging.info(f"현재 {len(client.guilds)}개의 서버에 연결됨")
        logging.info("------")

        # 자동 스케줄러 시작
        client.loop.create_task(scheduler_loop())

        # 미리 오늘자 데이터 캐싱 시도 (옵셔널)
        for guild in client.guilds:
            cfg = get_guild_settings(guild.id)
            if cfg and cfg.get("gemini_api_key"):
                client.loop.create_task(
                    get_today_horoscope_for_guild(guild.id, cfg["gemini_api_key"])
                )

        # 봇이 켜지면 딱 한 번만 데이터 로딩 시도
        for guild in client.guilds:
            cfg = get_guild_settings(guild.id)
            if cfg and cfg.get("gemini_api_key"):
                # 캐시가 없는 상태에서 딱 하나만 시도
                asyncio.create_task(get_today_horoscope_for_guild(guild.id, cfg["gemini_api_key"]))
                break

    except Exception as e:
        logging.error(f"on_ready 중 오류: {e}")


# --- /hello 테스트용 간단 명령 ---

@tree.command(name="hello", description="봇이 간단히 인사합니다.")
async def hello_command(interaction: discord.Interaction):
    try:
        await interaction.response.send_message("안녕! 🌙", ephemeral=True)
    except Exception as e:
        logging.error(f"/hello 처리 중 오류: {e}")


# --- /ohaasa 그룹 명령어 정의 ---

class Ohaasa(app_commands.Group):
    def __init__(self):
        super().__init__(name="ohaasa", description="오하아사 운세 관련 명령어")

    # /ohaasa channel
    @app_commands.command(
        name="channel",
        description="오하아사 운세를 게시할 채널을 설정합니다.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def channel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        target_channel = channel or interaction.channel
        cfg = get_or_create_guild_settings(interaction.guild.id)
        cfg["channel_id"] = target_channel.id
        save_guild_config()

        await interaction.response.send_message(
            f"✅ 이제 이 서버의 오하아사 운세는 {target_channel.mention} 에 게시됩니다.",
            ephemeral=True,
        )

    # /ohaasa apikey
    @app_commands.command(
        name="apikey",
        description="이 서버에서 사용할 Gemini API 키를 설정합니다.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def apikey(
        self,
        interaction: discord.Interaction,
        api_key: str,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        cfg = get_or_create_guild_settings(interaction.guild.id)
        cfg["gemini_api_key"] = api_key.strip()
        save_guild_config()

        await interaction.response.send_message(
            "✅ Gemini API 키를 저장했습니다.\n"
            "이 키는 `guild_config.json`에만 저장되며, 다른 사용자에게는 표시되지 않습니다.",
            ephemeral=True,
        )

    # /ohaasa time
    @app_commands.command(
        name="time",
        description="매일 자동으로 운세를 게시할 시간을 설정합니다. (24시간 기준, KST)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def time_cmd(
        self,
        interaction: discord.Interaction,
        hour: app_commands.Range[int, 0, 23],
        minute: app_commands.Range[int, 0, 59] = 0,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        cfg = get_or_create_guild_settings(interaction.guild.id)
        cfg["post_hour"] = int(hour)
        cfg["post_minute"] = int(minute)
        save_guild_config()

        await interaction.response.send_message(
            f"✅ 매일 **{hour:02d}:{minute:02d} (KST)** 에 자동으로 오하아사 운세를 게시하도록 설정했습니다.\n"
            "시간 기준은 **한국 표준시(KST, UTC+9)** 입니다.",
            ephemeral=True,
        )

    # /ohaasa mention
    @app_commands.command(
        name="mention",
        description="운세 게시 시 멘션 방식을 설정합니다.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        mode="멘션 방식을 선택하세요",
        role="멘션할 역할 (mode가 role일 때만 사용)",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="멘션 없음", value="none"),
            app_commands.Choice(name="@everyone", value="everyone"),
            app_commands.Choice(name="특정 역할 멘션", value="role"),
        ]
    )
    async def mention(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
        role: Optional[discord.Role] = None,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        cfg = get_or_create_guild_settings(interaction.guild.id)

        if mode.value == "role":
            if not role:
                await interaction.response.send_message(
                    "❌ `mode`가 `특정 역할 멘션`일 때는 `role` 인자를 반드시 지정해야 합니다.",
                    ephemeral=True,
                )
                return
            cfg["mention_mode"] = "role"
            cfg["mention_role_id"] = role.id
            msg = f"✅ 이제 오하아사 운세 게시 시 {role.mention} 을(를) 멘션합니다."
        elif mode.value == "everyone":
            cfg["mention_mode"] = "everyone"
            cfg["mention_role_id"] = None
            msg = "✅ 이제 오하아사 운세 게시 시 `@everyone` 을 멘션합니다."
        else:
            cfg["mention_mode"] = "none"
            cfg["mention_role_id"] = None
            msg = "✅ 이제 오하아사 운세 게시 시 멘션을 하지 않습니다."

        save_guild_config()
        await interaction.response.send_message(msg, ephemeral=True)

    # /ohaasa config
    @app_commands.command(
        name="config",
        description="현재 서버의 오하아사 자동 게시 설정을 확인합니다.",
    )
    async def config(
        self,
        interaction: discord.Interaction,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        cfg = get_or_create_guild_settings(interaction.guild.id)

        ch_id = cfg.get("channel_id")
        hour = cfg.get("post_hour")
        minute = cfg.get("post_minute")
        gemini_key = cfg.get("gemini_api_key")
        last_date = cfg.get("last_post_date")
        mention_mode = cfg.get("mention_mode", "none")
        mention_role_id = cfg.get("mention_role_id")

        channel_mention = (
            f"<#{ch_id}>" if ch_id else "아직 설정되지 않음 (`/ohaasa channel`)"
        )
        time_str = (
            f"{int(hour):02d}:{int(minute):02d}"
            if hour is not None and minute is not None
            else "아직 설정되지 않음 (`/ohaasa time`)"
        )
        gemini_status = "✅ 설정됨" if gemini_key else "❌ 설정되지 않음 (`/ohaasa apikey`)"
        last_post = last_date or "기록 없음"

        if mention_mode == "everyone":
            mention_str = "@everyone"
        elif mention_mode == "role" and mention_role_id:
            mention_str = f"<@&{int(mention_role_id)}>"
        else:
            mention_str = "멘션 없음"

        embed = discord.Embed(
            title="오하아사 자동 게시 설정",
            color=0x4E72B7,
        )
        embed.add_field(name="게시 채널", value=channel_mention, inline=False)
        embed.add_field(name="자동 게시 시간 (KST)", value=time_str, inline=False)
        embed.add_field(name="Gemini API 키", value=gemini_status, inline=False)
        embed.add_field(name="멘션 설정", value=mention_str, inline=False)
        embed.add_field(name="마지막 자동 게시 날짜 (KST)", value=last_post, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # /ohaasa test (서버 소유자만)
    @app_commands.command(
        name="test",
        description="지금 바로 오하아사 운세를 테스트로 게시합니다. (서버 소유자만)",
    )
    @is_guild_owner()
    async def test(
        self,
        interaction: discord.Interaction,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        cfg = get_or_create_guild_settings(interaction.guild.id)
        ch_id = cfg.get("channel_id")
        gemini_key = cfg.get("gemini_api_key")

        if not ch_id:
            await interaction.response.send_message(
                "❌ 게시 채널이 설정되어 있지 않습니다.\n"
                "`/ohaasa channel` 으로 먼저 채널을 설정해 주세요.",
                ephemeral=True,
            )
            return

        if not gemini_key:
            await interaction.response.send_message(
                "❌ Gemini API 키가 설정되어 있지 않습니다.\n"
                "`/ohaasa apikey` 명령으로 키를 설정해 주세요.",
                ephemeral=True,
            )
            return

        channel = client.get_channel(int(ch_id))
        if not channel:
            await interaction.response.send_message(
                f"❌ 설정된 채널 <#{ch_id}> 을(를) 찾을 수 없습니다. "
                "`/ohaasa channel` 으로 다시 설정해 주세요.",
                ephemeral=True,
            )
            return

        # 멘션 텍스트 구성
        mention_text: Optional[str] = None
        mode = cfg.get("mention_mode", "none")
        role_id = cfg.get("mention_role_id")

        if mode == "everyone":
            mention_text = "@everyone"
        elif mode == "role" and role_id:
            mention_text = f"<@&{int(role_id)}>"

        await interaction.response.send_message(
            f"✅ {channel.mention} 에 오늘의 오하아사 운세를 테스트로 게시합니다.",
            ephemeral=True,
        )

        await fetch_and_post_horoscope(
            channel,
            gemini_key,
            mention_text,
            interaction.guild.id,
        )


# 그룹을 트리에 등록
ohaasa_group = Ohaasa()
tree.add_command(ohaasa_group)


# --- 퍼미션 에러 핸들링 ---

@ohaasa_group.channel.error
@ohaasa_group.apikey.error
@ohaasa_group.time_cmd.error
@ohaasa_group.mention.error
async def perms_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ 이 명령어는 `서버 관리하기` 권한이 있는 사용자만 사용할 수 있습니다.",
            ephemeral=True,
        )
    else:
        logging.error(f"슬래시 커맨드 에러: {error}")


# --- 실행 진입점 ---

if __name__ == "__main__":
    load_guild_config()
    try:
        client.run(DISCORD_BOT_TOKEN)
    except discord.errors.LoginFailure:
        logging.error("오류: 디스코드 봇 토큰이 잘못되었습니다.")
    except Exception as e:
        logging.error(f"봇 실행 중 예기치 않은 오류 발생: {e}")
