import asyncio
import os
from pathlib import Path
from db_manager import AsyncMySQLManager
import config
from speech_recognizer import recognize_audio

async def process_all_milestones():
    db = AsyncMySQLManager(config.db_config)
    await db.ensure_database()
    await db.create_pool()
    await db.init_tables()

    devices = await db.fetchall("SELECT id, mac_address FROM devices")
    for dev in devices:
        device_id = dev['id']
        mac = dev['mac_address']
        safe_mac = mac.replace(':', '-')

        milestones = await db.fetchall(
            "SELECT milestone_number, question_count FROM milestones WHERE device_id = %s AND question_count > 0",
            (device_id,)
        )

        for milestone in milestones:
            milestone_num = milestone['milestone_number']
            question_count = milestone['question_count']

            for q_idx in range(1, question_count + 1):
                # 修复：使用 answer 作为文件名前缀，目录为 anwser
                audio_path = Path(f"task/milestones{milestone_num}/{safe_mac}/answer/answer{q_idx}.wav")
                if not audio_path.exists():
                    print(f"⚠️ 跳过缺失文件: {audio_path}")
                    continue

                print(f"🎤 识别: {audio_path}")
                try:
                    text = recognize_audio(str(audio_path))
                    print(f"✅ 识别结果: {text}")

                    await db.execute(
                        """
                        INSERT INTO milestone_answers
                        (device_id, milestone_number, question_index, answer_text, answer_audio_path)
                        VALUES (%s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE answer_text = VALUES(answer_text)
                        """,
                        (device_id, milestone_num, q_idx, text, str(audio_path))
                    )
                except Exception as e:
                    print(f"❌ 识别失败 {audio_path}: {e}")

    await db.close_pool()
    print("🎉 批量识别完成")

if __name__ == "__main__":
    asyncio.run(process_all_milestones())