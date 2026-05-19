import os
import asyncio
import base64
import argparse
import aiohttp
import aiofiles
from dotenv import load_dotenv
from volcenginesdkarkruntime import Ark
from volcenginesdkarkruntime.types.images.images import SequentialImageGenerationOptions
from db_manager import AsyncMySQLManager
import config
from persona_generator import async_create_response

load_dotenv()

API_KEY = os.getenv('API_KEY')
if not API_KEY:
    raise RuntimeError("请在 .env 文件中设置 API_KEY")

image_client = Ark(
    base_url="https://ark.cn-beijing.volces.com/api/v3",
    api_key=API_KEY,
)

PICTURE_ROOT = "picture"

async def generate_image_prompt(persona_description: str) -> str:
    """
    利用大语言模型将人物画像转换为详细的图像生成提示词，
    并强调查找参考图片中儿童的面部特征、发型、五官等必须保持不变。
    """
    instruction = (
        "你是一个专业的文生图提示词工程师。请根据以下儿童人物画像，生成一段详细的图像生成提示词（中文），"
        "用于 AI 绘画。\n"
        "【极其重要的要求】\n"
        "- 生成的人物必须与参考图片中的儿童为同一人，面部特征、发型、五官细节必须完全一致，绝对不能改变。\n"
        "- 只允许改变背景、动作、表情、肢体语言、服装（如果画像有提到可以调整）。\n"
        "- 提示词中必须明确强调“保持原图人脸不变”，例如“same face as the reference image”，“identity preserved”。\n"
        "- 不要改变儿童的性别、年龄、面部轮廓。\n\n"
        "其他要求：\n"
        "- 根据画像中的性格、情感和兴趣，描述儿童的表情、动作、肢体语言。\n"
        "- 设计与画像中情感底色和兴趣宇宙相匹配的背景（例如：游乐园、森林、书房、海边等）。\n"
        "- 整体风格温暖、自然、明亮，如同高质量写真。\n"
        f"人物画像：\n{persona_description}\n\n"
        "只输出提示词，提示词中应包含上述重要要求，不要附加任何解释。"
    )
    response = await async_create_response(
        model="deepseek-v3-2-251201",
        input_messages=[{"role": "user", "content": instruction}]
    )
    prompt = ""
    if response.output:
        for item in response.output:
            if hasattr(item, 'content'):
                for part in item.content:
                    if part.type == 'output_text':
                        prompt += part.text
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("LLM 未能生成有效的图像提示词")
    # 可选：再次追加强制保持身份的短语
    if "保持" not in prompt and "same face" not in prompt.lower():
        prompt = prompt + "（必须保持参考图片中儿童的面部、发型完全一致）"
    print(f"✨ 生成的图像提示词（长度 {len(prompt)}）:\n{prompt[:300]}...")
    return prompt

async def generate_and_save_images(mac: str, db_manager: AsyncMySQLManager, custom_prompt: str = None):
    device = await db_manager.get_device_by_mac(mac)
    if not device:
        raise ValueError(f"设备 {mac} 不存在于数据库中，请先确保设备已注册")
    
    persona_description = device.get('persona_description', '').strip()
    
    if custom_prompt:
        prompt = custom_prompt
        print(f"🎨 使用自定义提示词，长度: {len(prompt)}")
    else:
        if not persona_description:
            raise ValueError(f"设备 {mac} 没有人物画像，无法生成个性化图片。请先运行人物画像生成。")
        prompt = await generate_image_prompt(persona_description)
        print(f"🎨 已根据人物画像生成提示词，长度: {len(prompt)} 字符")
    
    safe_mac = mac.replace(':', '-')
    device_dir = os.path.join(PICTURE_ROOT, safe_mac)
    src_dir = os.path.join(device_dir, "src")
    ai_dir = os.path.join(device_dir, "AI_Generat")
    src_image_path = os.path.join(src_dir, "user.png")
    output_image_path = os.path.join(ai_dir, "user.png")
    
    os.makedirs(ai_dir, exist_ok=True)
    
    if not os.path.exists(src_image_path):
        raise FileNotFoundError(f"参考图片不存在: {src_image_path}")
    
    with open(src_image_path, "rb") as f:
        image_data = f.read()
    base64_image = base64.b64encode(image_data).decode('utf-8')
    data_url = f"data:image/png;base64,{base64_image}"
    
    print(f"📡 调用图像生成 API，MAC={mac}，提示词长度: {len(prompt)}")
    print("⚠️ 注意：当前使用的通用文生图模型可能无法严格保持原图人脸的完全一致。如需更好的身份保持效果，建议使用 Stable Diffusion + InstantID / IP-Adapter 等专用模型。")
    
    imagesResponse = image_client.images.generate(
        model="doubao-seedream-4-0-250828",
        prompt=prompt,
        image=[data_url],
        size="2K",
        sequential_image_generation="auto",
        sequential_image_generation_options=SequentialImageGenerationOptions(max_images=1),
        response_format="url",
        watermark=True
    )
    
    if not imagesResponse.data:
        raise RuntimeError("API 未返回任何图片")
    
    async with aiohttp.ClientSession() as session:
        for idx, image_obj in enumerate(imagesResponse.data, start=1):
            image_url = image_obj.url
            save_path = output_image_path if idx == 1 else os.path.join(ai_dir, f"user_{idx}.png")
            async with session.get(image_url) as resp:
                if resp.status != 200:
                    print(f"⚠️ 下载第 {idx} 张图片失败，HTTP {resp.status}")
                    continue
                image_bytes = await resp.read()
                async with aiofiles.open(save_path, 'wb') as f:
                    await f.write(image_bytes)
                print(f"✅ 图片已保存: {save_path}")
    
    preset_photo_rel = os.path.join(PICTURE_ROOT, safe_mac, "src", "user.png")
    ai_photo_rel = os.path.join(PICTURE_ROOT, safe_mac, "AI_Generat", "user.png")
    await db_manager.update_preset_photo(mac, preset_photo_rel)
    await db_manager.update_ai_photo(mac, ai_photo_rel)
    print(f"✅ 数据库已更新: preset_photo_url={preset_photo_rel}, ai_generated_photo_url={ai_photo_rel}")

async def main():
    parser = argparse.ArgumentParser(description="根据 MAC 地址，使用人物画像生成 AI 图片并更新数据库")
    parser.add_argument("--mac", type=str, required=True, help="设备的 MAC 地址（如 F0:9E:9E:22:22:DC）")
    parser.add_argument("--prompt", type=str, default=None, help="自定义提示词（完全覆盖自动生成的提示词）")
    args = parser.parse_args()
    
    db_manager = AsyncMySQLManager(config.db_config)
    await db_manager.ensure_database()
    await db_manager.create_pool()
    await db_manager.init_tables()
    
    try:
        await generate_and_save_images(args.mac, db_manager, args.prompt)
    finally:
        await db_manager.close_pool()

if __name__ == "__main__":
    asyncio.run(main())