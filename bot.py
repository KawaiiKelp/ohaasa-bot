# --- 라이브러리 임포트 ---
import os
import json
import time
import asyncio
import logging
import datetime as dt
from typing import Dict, Any, Optional

import discord
from discord import app_commands
from dotenv import load_dotenv
import requests
import aiohttp

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
    "models/gemini-2.5-flash-preview-09-2025:generateContent"
)

GUILD_CONFIG_PATH = "guild_config.json"

# --- 디스코드 클라이언트 ---
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# guild_id(int) -> 설정 dict
guild_settings: Dict[int, Dict[str, Any]] = {}

# 길드별 오늘 운세 캐시: { guild_id: { "date": "YYYYMMDD", "data": [ ...translated... ] } }
horoscope_cache: Dict[int, Dict[str, Any]] = {}
cache_lock = asyncio.Lock()


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
            "post_hour": 8,          # 기본 자동 발사 시간: 08:00
            "post_minute": 0,
            "gemini_api_key": "",
            "last_post_date": None,  # YYYYMMDD
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
    japanese_json_text: str,
    gemini_api_key: str,
    max_retries: int = 3,
) -> Optional[Any]:
    """
    Gemini API를 사용해 일본어 운세 JSON 문자열을
    한국어로 번역된 JSON(List[Object])으로 반환한다.
    500에러 등 서버 내부 오류 시 자동 재시도.
    """
    if not gemini_api_key:
        logging.error("Gemini API 키가 설정되어 있지 않습니다.")
        return None

    system_prompt = (
        "You are an expert translator specializing in Japanese-to-Korean horoscopes. "
        "The input is a JSON string containing horoscope rankings and descriptions in Japanese. "
        "Translate ALL Japanese text into natural, easy-to-read Korean. "
        "Keep the structure (rank, sign, description) and output a JSON array of objects with "
        "fields: rank, sign_ko, description_ko. "
        "Return ONLY the raw JSON array."
    )

    response_schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "rank": {
                    "type": "STRING",
                    "description": "Ranking in Korean, e.g. '1위'"
                },
                "sign_ko": {
                    "type": "STRING",
                    "description": "Korean name of the zodiac sign, e.g. '양자리'"
                },
                "description_ko": {
                    "type": "STRING",
                    "description": "Full horoscope description in Korean"
                },
            },
            "required": ["rank", "sign_ko", "description_ko"],
        },
    }

    payload = {
        "contents": [{"parts": [{"text": japanese_json_text}]}],
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
                async with session.post(
                    f"{GEMINI_API_URL}?key={gemini_api_key}",
                    headers=headers,
                    json=payload,
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        json_string = result["candidates"][0]["content"]["parts"][0]["text"]
                        return json.loads(json_string)

                    # 5xx → 재시도 대상
                    if 500 <= resp.status < 600:
                        error_text = await resp.text()
                        logging.error(
                            f"Gemini API 서버 오류 (Status {resp.status}, 시도 {attempt+1}/{max_retries}): {error_text}"
                        )
                        if attempt < max_retries - 1:
                            await asyncio.sleep(1 + attempt)  # 백오프
                            continue
                        return None

                    # 그 외 상태코드는 재시도하지 않고 종료
                    error_text = await resp.text()
                    logging.error(
                        f"Gemini API 오류 (Status {resp.status}): {error_text}"
                    )
                    return None

        except Exception as e:
            logging.error(f"Gemini 번역 함수 예외 (시도 {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(1 + attempt)
                continue
            return None

    return None


# --- 오하아사 JSON 가져오기 ---

def fetch_horoscope_data_sync() -> Optional[str]:
    """
    오하아사 공식 JSON API에서 오늘자 운세 데이터를 가져와
    일본어 JSON 문자열로 반환한다.
    """
    logging.info("운세 데이터(JSON) 가져오기 시작")

    SIGN_CODE_TO_JP = {
        "01": "牡羊座",
        "02": "牡牛座",
        "03": "双子座",
        "04": "蟹座",
        "05": "獅子座",
        "06": "乙女座",
        "07": "天秤座",
        "08": "蠍座",
        "09": "射手座",
        "10": "山羊座",
        "11": "水瓶座",
        "12": "魚座",
    }

    try:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/javascript,*/*;q=0.01",
            "Referer": OHAASA_URL,
        }
        resp = requests.get(OHAASA_JSON_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        logging.info("JSON API 접속 성공")

        data = resp.json()

        if not isinstance(data, list) or not data:
            logging.error("JSON 최상위 구조가 기대와 다릅니다 (list가 아니거나 비어 있음).")
            return None

        root = data[0]
        details = root.get("detail", [])
        logging.info(f"JSON에서 detail 항목 {len(details)}개 발견.")

        if len(details) != 12:
            logging.warning(f"경고: detail 개수가 12개가 아닙니다. 실제 개수: {len(details)}")

        result = []

        for idx, d in enumerate(details):
            try:
                rank_str = d.get("ranking_no")
                sign_code = d.get("horoscope_st")
                text = d.get("horoscope_text")

                if not (rank_str and sign_code and text):
                    logging.warning(f"{idx}번째 detail에 필요한 필드가 없습니다: {d}")
                    continue

                rank = f"{rank_str}位"
                sign_jp = SIGN_CODE_TO_JP.get(sign_code, f"不明な星座({sign_code})")
                description = text.replace("\t", " ").strip()

                result.append(
                    {
                        "rank": rank,
                        "sign_jp": sign_jp,
                        "description_jp": description,
                    }
                )

            except Exception as e:
                logging.error(f"{idx}번째 detail 처리 중 오류: {e}")
                continue

        if not result:
            logging.error("JSON에서 유효한 운세 데이터를 하나도 만들지 못했습니다.")
            return None

        if len(result) != 12:
            logging.warning(
                f"경고: 12개가 아닌 {len(result)}개의 운세만 수집되었습니다."
            )

        return json.dumps(result, ensure_ascii=False, indent=2)

    except requests.exceptions.RequestException as e:
        logging.error(f"운세 JSON API 요청 중 오류: {e}")
        return None
    except Exception as e:
        logging.error(f"운세 JSON 처리 중 알 수 없는 오류: {e}")
        return None

async def get_today_horoscope_for_guild(
    guild_id: int,
    gemini_api_key: str,
) -> Optional[Any]:
    """
    해당 길드 기준으로 '오늘자 번역된 운세'를 가져온다.
    - 이미 오늘자 데이터가 캐시에 있으면 그대로 반환
    - 없으면 JSON 요청 + Gemini 번역 후 캐시에 넣고 반환
    """
    today = time.strftime("%Y%m%d", time.localtime())

    # 1) 캐시 확인 (락 잡고 짧게)
    async with cache_lock:
        cached = horoscope_cache.get(guild_id)
        if cached and cached.get("date") == today and cached.get("data"):
            logging.info(f"길드 {guild_id} 캐시된 운세 사용")
            return cached["data"]

    # 2) 캐시에 없으면 새로 로드 + 번역
    logging.info(f"길드 {guild_id} 오늘자 운세 최초 로드 시작")

    japanese_json_data = await asyncio.to_thread(fetch_horoscope_data_sync)
    if not japanese_json_data or japanese_json_data == "[]":
        logging.error("운세 JSON 로드 실패")
        return None

    translated_data = await translate_text(japanese_json_data, gemini_api_key)
    if not translated_data:
        logging.error("Gemini 번역 실패")
        return None

    # 3) 캐시에 저장
    async with cache_lock:
        horoscope_cache[guild_id] = {
            "date": today,
            "data": translated_data,
        }

    return translated_data


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
    """
    loading_content = "✨ **[오하아사 별자리 운세]** 데이터를 가져오는 중입니다..."
    if mention_text:
        loading_content = f"{mention_text} {loading_content}"

    loading_message = await channel.send(loading_content)

    # 1+2. 캐시 포함 '오늘자 번역된 운세' 가져오기
    if guild_id is None and hasattr(channel, "guild") and channel.guild:
        guild_id = channel.guild.id

    if guild_id is None:
        await loading_message.edit(
            content="❌ 길드 정보를 찾을 수 없어 운세를 불러오지 못했습니다."
        )
        return

    translated_data = await get_today_horoscope_for_guild(guild_id, gemini_api_key)

    if not translated_data:
        await loading_message.edit(
            content="❌ 오늘자 운세 데이터를 불러오지 못했습니다. (JSON 또는 Gemini 오류)"
        )
        return


    # 3. 디스코드 Embed + 스레드로 게시
    try:
        date_str = time.strftime("%Y년 %m월 %d일", time.localtime())

        embed = discord.Embed(
            title=f"📅 {date_str} 오늘의 오하아사 별자리 랭킹",
            description=f"[원문 출처: 아사히 방송 오하아사](<{OHAASA_URL}>)",
            color=0x4E72B7,
        )

        top_rankings = translated_data[:6]
        bottom_rankings = translated_data[6:]

        top_list = "\n".join(
            f"**{item['rank']}** — {item['sign_ko']}" for item in top_rankings
        )
        bottom_list = "\n".join(
            f"**{item['rank']}** — {item['sign_ko']}" for item in bottom_rankings
        )

        embed.add_field(
            name="🥇 상위 랭킹 (1위 ~ 6위)", value=top_list or "데이터 없음", inline=True
        )
        embed.add_field(
            name="⬇️ 하위 랭킹 (7위 ~ 12위)",
            value=bottom_list or "데이터 없음",
            inline=True,
        )

        await loading_message.edit(content=None, embed=embed)
        initial_message = loading_message

        # 상세 내용 스레드 생성
        try:
            thread = await initial_message.create_thread(
                name=f"{date_str} 별자리 운세 상세 내용",
                auto_archive_duration=60,  # 1시간 후 자동 보관
            )
            logging.info(f"스레드 생성 성공: {thread.name}")
        except discord.Forbidden:
            thread = channel
            logging.warning(
                "스레드 생성 권한이 없어, 상세 내용을 현재 채널에 직접 게시합니다."
            )
        except Exception as e:
            thread = channel
            logging.error(f"스레드 생성 중 예기치 않은 오류: {e}")

        # 상세 내용 텍스트
        top_details_text = "**🥇 1위 ~ 6위 상세 운세**\n"
        for item in top_rankings:
            top_details_text += (
                f"\n**{item['rank']} {item['sign_ko']}**\n"
                f"> {item['description_ko']}\n"
            )

        bottom_details_text = "**⬇️ 7위 ~ 12위 상세 운세**\n"
        for item in bottom_rankings:
            bottom_details_text += (
                f"\n**{item['rank']} {item['sign_ko']}**\n"
                f"> {item['description_ko']}\n"
            )

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
    """
    await client.wait_until_ready()
    logging.info("자동 운세 게시 스케줄러 시작")

    while not client.is_closed():
        now = dt.datetime.now()
        today_str = now.strftime("%Y%m%d")

        for guild_id, cfg in guild_settings.items():
            channel_id = cfg.get("channel_id")
            hour = cfg.get("post_hour")
            minute = cfg.get("post_minute")
            gemini_key = cfg.get("gemini_api_key")
            last_post_date = cfg.get("last_post_date")

            if channel_id is None or gemini_key is None:
                continue

            if last_post_date == today_str:
                continue

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
                    f"길드 {guild_id}에 대해 자동 운세 게시 실행 (채널 {channel_id})"
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

        client.loop.create_task(scheduler_loop())

    except Exception as e:
        logging.error(f"on_ready 중 오류: {e}")
        
    for guild in client.guilds:
        cfg = get_guild_settings(guild.id)
        if cfg and cfg.get("gemini_api_key"):
            client.loop.create_task(
                get_today_horoscope_for_guild(guild.id, cfg["gemini_api_key"])
            )


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
        description="매일 자동으로 운세를 게시할 시간을 설정합니다. (24시간 기준)",
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
            f"✅ 매일 **{hour:02d}:{minute:02d}** 에 자동으로 오하아사 운세를 게시하도록 설정했습니다.\n"
            "시간 기준은 **봇이 실행 중인 서버의 로컬 시간**입니다.",
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
        embed.add_field(name="자동 게시 시간", value=time_str, inline=False)
        embed.add_field(name="Gemini API 키", value=gemini_status, inline=False)
        embed.add_field(name="멘션 설정", value=mention_str, inline=False)
        embed.add_field(name="마지막 자동 게시 날짜", value=last_post, inline=False)

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
