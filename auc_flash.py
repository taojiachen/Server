import json
import time
import uuid
import requests
import base64
import os  # 补充导入 os 模块
from dotenv import load_dotenv

load_dotenv()

# 辅助函数：将本地文件转换为Base64
def file_to_base64(file_path):
    with open(file_path, 'rb') as file:
        file_data = file.read()
        base64_data = base64.b64encode(file_data).decode('utf-8')
    return base64_data

# recognize_task 函数：仅支持本地文件路径（file_path）
def recognize_task(file_path):
    recognize_url = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
    appid = os.getenv("APPID")
    token = os.getenv("ACCESS_TOKEN")

    headers = {
        "X-Api-App-Key": appid,
        "X-Api-Access-Key": token,
        "X-Api-Resource-Id": "volc.bigasr.auc_turbo",
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Sequence": "-1",
    }

    # 仅支持 file_path，不再检查 file_url
    if not file_path:
        raise ValueError("必须提供 file_path")

    base64_data = file_to_base64(file_path)
    audio_data = {"data": base64_data}

    request = {
        "user": {
            "uid": appid
        },
        "audio": audio_data,
        "request": {
            "model_name": "bigmodel",
            # "enable_itn": True,
            # "enable_punc": True,
            # "enable_ddc": True,
            # "enable_speaker_info": False,
        },
    }

    response = requests.post(recognize_url, json=request, headers=headers)
    if 'X-Api-Status-Code' in response.headers:
        print(f'recognize task response header X-Api-Status-Code: {response.headers["X-Api-Status-Code"]}')
        print(f'recognize task response header X-Api-Message: {response.headers["X-Api-Message"]}')
        print(time.asctime() + " recognize task response header X-Tt-Logid: {}".format(response.headers["X-Tt-Logid"]))
        print(f'recognize task response content is: {response.json()}\n')
    else:
        print(f'recognize task failed and the response headers are: {response.headers}\n')
        exit(1)
    return response

# recognizeMode：仅接收 file_path
def recognizeMode(file_path):
    start_time = time.time()
    print(time.asctime() + " START!")
    recognize_response = recognize_task(file_path=file_path)
    code = recognize_response.headers['X-Api-Status-Code']
    logid = recognize_response.headers['X-Tt-Logid']
    if code == '20000000':  # task finished
        with open("result.json", mode='w', encoding='utf-8') as f:
            f.write(json.dumps(recognize_response.json(), indent=4, ensure_ascii=False))
        print(time.asctime() + " SUCCESS! \n")
        print(f"程序运行耗时: {time.time() - start_time:.6f} 秒")
    elif code != '20000001' and code != '20000002':  # task failed
        print(time.asctime() + " FAILED! code: {}, logid: {}".format(code, logid))

def main():
    # 仅使用本地文件路径模式
    file_path = "audio/example.mp3"  # 请修改为你的实际音频文件路径
    recognizeMode(file_path=file_path)

if __name__ == '__main__':
    main()