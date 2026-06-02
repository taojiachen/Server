import asyncio
import os
import sys
import json
import base64
import requests
import traceback
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

# 添加项目根目录到 sys.path（用于导入 config 和 db_manager）
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# ==================== TTS 配置 ====================
TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse"
TTS_AUDIO_FORMAT = "ogg_opus"      # 与 ESP 设备期望格式一致
TTS_SAMPLE_RATE = 24000
TTS_SPEAKER = os.getenv("TTS_SPEAKER", "zh_female_wenroumama_uranus_bigtts")
FORCE_REGENERATE = False            # 是否强制重新生成已存在的音频文件

# ==================== TTS 核心函数（从 tts_http.py 合并） ====================
def parse_event(stream):
    """解析 SSE 流事件"""
    event = {"event": "", "data": ""}
    for raw_line in stream:
        line = raw_line.decode("utf-8").strip()
        if line == "":
            if event["data"]:
                event["data"] = event["data"].rstrip("\n")
                yield event
            event = {"event": "message", "data": ""}
            continue
        if line.startswith(":"):
            continue
        if ":" in line:
            field, value = line.split(":", 1)
            value = value.lstrip()
            if field == "data":
                event["data"] += value + "\n"
            elif field == "event":
                event["event"] = value
    if event["data"]:
        event["data"] = event["data"].rstrip("\n")
        yield event


def tts_http_sse_stream(url, headers, params, audio_save_path):
    """
    调用字节跳动 TTS 服务，生成音频文件。
    内部是同步请求，会直接写入文件。
    """
    session = requests.Session()
    try:
        response = session.post(url, headers=headers, json=params, stream=True)
        print(f"TTS 响应状态码: {response.status_code}")
        logid = response.headers.get('X-Tt-Logid')
        if logid:
            print(f"X-Tt-Logid: {logid}")

        audio_data = bytearray()
        for event_data in parse_event(response.iter_lines()):
            if not event_data:
                continue
            data = json.loads(event_data['data'])
            if data.get("code", 0) == 0 and "data" in data and data["data"]:
                chunk_audio = base64.b64decode(data["data"])
                audio_data.extend(chunk_audio)
                continue
            if data.get("code", 0) == 20000000:
                if 'usage' in data:
                    print("usage:", data['usage'])
                break
            if data.get("code", 0) > 0:
                print(f"TTS 错误响应: {data}")
                break

        if audio_data:
            os.makedirs(os.path.dirname(audio_save_path), exist_ok=True)
            with open(audio_save_path, "wb") as f:
                f.write(audio_data)
            print(f"✅ TTS 音频已保存: {audio_save_path} ({len(audio_data)/1024:.2f} KB)")
        else:
            raise RuntimeError("TTS 返回的音频数据为空")
    except Exception as e:
        print(f"❌ TTS 请求失败: {e}")
        traceback.print_exc()
        raise
    finally:
        response.close()
        session.close()


def generate_audio_sync(text: str, output_path: str, speaker: str = TTS_SPEAKER) -> bool:
    """同步生成单个音频文件，返回是否成功"""
    load_dotenv()
    appID = os.getenv("APPID")
    accessKey = os.getenv("ACCESS_KEY")
    if not appID or not accessKey:
        raise RuntimeError("请在 .env 文件中设置 APPID 和 ACCESS_KEY")

    headers = {
        "X-Api-App-Id": appID,
        "X-Api-Access-Key": accessKey,
        "X-Api-Resource-Id": "seed-tts-2.0",
        "Content-Type": "application/json",
        "Connection": "keep-alive",
    }
    payload = {
        "user": {"uid": "milestone_generator"},
        "req_params": {
            "text": text,
            "speaker": "zh_female_wenroumama_uranus_bigtts",
            "audio_params": {
                "format": TTS_AUDIO_FORMAT,
                "sample_rate": TTS_SAMPLE_RATE,
                "bit_rate": 128,
                "enable_timestamp": False,
            },
            "additions": "{\"explicit_language\":\"zh\",\"disable_markdown_filter\":true}"
        }
    }
    try:
        tts_http_sse_stream(TTS_URL, headers, payload, output_path)
        return True
    except Exception:
        return False


async def generate_audio_async(text: str, output_path: str, speaker: str = TTS_SPEAKER) -> bool:
    """异步包装同步 TTS 调用"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, generate_audio_sync, text, output_path, speaker)


# ==================== 数据库读取与批量生成逻辑 ====================
async def generate_all_milestone_audios(db_manager):
    """
    读取数据库中所有里程碑的问题，生成对应的 Opus 音频文件。
    文件保存路径：task/milestones{里程碑编号}/question/{序号}.opus
    若文件已存在且 FORCE_REGENERATE=False 则跳过。
    """
    rows = await db_manager.fetchall("""
        SELECT device_id, milestone_number, questions
        FROM milestones
        WHERE questions IS NOT NULL AND JSON_LENGTH(questions) > 0
    """)

    if not rows:
        print("⚠️ 没有找到任何包含问题的里程碑记录。")
        return

    # 去重：同一里程碑编号 + 相同问题列表只生成一次
    seen = set()
    for row in rows:
        milestone_num = row['milestone_number']
        questions_json = row['questions']
        try:
            questions = json.loads(questions_json)
        except json.JSONDecodeError:
            print(f"⚠️ 里程碑 {milestone_num} 的 questions 不是合法 JSON: {questions_json}")
            continue
        if not isinstance(questions, list) or not questions:
            continue

        key = (milestone_num, json.dumps(questions, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)

        # 创建输出目录
        output_dir = Path(f"task/milestones{milestone_num}/question")
        output_dir.mkdir(parents=True, exist_ok=True)

        # 为每个问题生成音频
        for idx, question_text in enumerate(questions, start=1):
            output_file = output_dir / f"{idx}.opus"
            if not FORCE_REGENERATE and output_file.exists():
                print(f"⏭️ 已存在，跳过: {output_file}")
                continue

            print(f"🎤 生成音频 [里程碑{milestone_num} 问题{idx}]: {question_text[:60]}...")
            success = await generate_audio_async(question_text, str(output_file))
            if success:
                print(f"✅ 已保存: {output_file}")
            else:
                print(f"❌ 生成失败: {output_file}")

    print("🎉 所有里程碑音频生成任务完成！")


async def generate_milestone_audio_for_device(db_manager, device_id: int, milestone_number: int, questions: list):
    """
    为指定设备的指定里程碑生成音频（不依赖全局去重，直接按设备+里程碑生成）。
    通常用于 API 动态添加问题时实时生成。
    """
    output_dir = Path(f"task/milestones{milestone_number}/question")
    output_dir.mkdir(parents=True, exist_ok=True)

    for idx, question_text in enumerate(questions, start=1):
        output_file = output_dir / f"{idx}.opus"
        if not FORCE_REGENERATE and output_file.exists():
            print(f"⏭️ 已存在，跳过: {output_file}")
            continue
        print(f"🎤 生成设备 {device_id} 里程碑 {milestone_number} 问题 {idx} 音频...")
        success = await generate_audio_async(question_text, str(output_file))
        if success:
            print(f"✅ 已保存: {output_file}")
        else:
            print(f"❌ 生成失败: {output_file}")