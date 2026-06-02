from speech_recognizer import recognize_audio

audio_path = r"task/milestones1/10-51-DB-84-C4-48/answer/answer1.wav"

try:
    text = recognize_audio(audio_path)
    print(f"识别结果: {text}")
except Exception as e:
    print(f"错误: {e}")