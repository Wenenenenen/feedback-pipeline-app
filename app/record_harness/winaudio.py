# -*- coding: utf-8 -*-
"""本机桌面录屏的系统声音录制（Windows 的 Python 执行，需要 pyaudiowpatch）。

用法：python winaudio.py <输出.wav>
用 WASAPI 回环录下默认输出设备正在播放的声音，写成 wav；标准输入收到 q 或关闭时停止。
标准输出：{"event": "audio-start", "wall": <第一帧对应的墙钟秒数>}，结束时 {"event": "audio-stop"}；
画面那边 ffmpeg 用 -use_wallclock_as_timestamps 打墙钟时间戳，合成时按两者的起始时刻对齐。
WASAPI 回环在没有声音播放时不出数据，录制期间同时往默认输出写静音，保证时间轴连续、与画面对得上。
"""
import json
import sys
import threading
import time
import wave

import pyaudiowpatch as pyaudio

CHUNK = 1024


def emit(obj: dict):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def main():
    out = sys.argv[1]
    p = pyaudio.PyAudio()
    try:
        dev = p.get_default_wasapi_loopback()
        rate = int(dev['defaultSampleRate'])
        ch = max(1, min(2, int(dev['maxInputChannels'])))
        stop = threading.Event()

        # 静音流：让音频引擎持续运转，回环才会连续出数据
        def keep_alive():
            o = p.open(format=pyaudio.paInt16, channels=ch, rate=rate, output=True)
            silence = b'\0' * (CHUNK * ch * 2)
            while not stop.is_set():
                o.write(silence)
            o.stop_stream()
            o.close()
        threading.Thread(target=keep_alive, daemon=True).start()

        def wait_quit():
            for line in sys.stdin:
                if line.strip() == 'q':
                    break
            stop.set()
        threading.Thread(target=wait_quit, daemon=True).start()

        s = p.open(format=pyaudio.paInt16, channels=ch, rate=rate, input=True,
                   input_device_index=dev['index'], frames_per_buffer=CHUNK)
        with wave.open(out, 'wb') as w:
            w.setnchannels(ch)
            w.setsampwidth(2)
            w.setframerate(rate)
            first = True
            while not stop.is_set():
                data = s.read(CHUNK, exception_on_overflow=False)
                if first:
                    # 读到第一块时，这块的开头已经过去了一块的时长
                    emit({'event': 'audio-start', 'wall': time.time() - CHUNK / rate,
                          'rate': rate, 'channels': ch, 'device': dev['name']})
                    first = False
                w.writeframes(data)
        s.stop_stream()
        s.close()
    finally:
        p.terminate()
    emit({'event': 'audio-stop'})


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
