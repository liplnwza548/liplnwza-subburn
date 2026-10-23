# RUNBOOK — Burn Thai Viral Pop (script-aligned v1.1)

สำหรับ COS / Muse / Hermes mini PC ใช้ซ้ำทุกครั้ง

## กฎเหล็ก
1. บท Lip = ข้อความบนจอ (verbatim) — ห้ามใช้คำจาก STT เป็นซับ
2. STT / `--words-json` = เวลาอย่างเดียว
3. mismatch → exit 2 รายงานทุกจุด ห้ามเบิร์น ถาม Lip รวดเดียว
4. สไตล์ v1: Viral Pop เท่านั้น
5. ffmpeg ใช้ `ass=` + fontsdir ชื่อไฟล์เปล่า — ห้าม `subtitles=` ห้ามพาธ C:/ ในฟิลเตอร์

## อินพุตที่ต้องมี
- `video`: คลิปตัดเสร็จแล้ว (mp4/mov) 1080×1920
- `script`: `.txt` หนึ่งท่อนต่อหนึ่งบรรทัด (ตามที่อยากให้ขึ้นจอ)
- `fonts`: โฟลเดอร์มี Prompt-Bold.ttf (ในสกิลมี `fonts/`)
- `job.json` (ถ้าต้องการคำเหลือง): มีรายการ keywords
- `GROQ_API_KEY` ใน `.env` ของ work root (ถ้าไม่มี `--words-json`)

## คำสั่งมาตรฐาน (Linux / Muse บนเครื่อง COS)

```bash
source /home/box/subtitle-work/.venv/bin/activate
cd /home/box/tools/animated-subtitle-renderer-v2

python scripts/burn_script_aligned.py \
  --video /path/to/clip.mp4 \
  --script /path/to/script.txt \
  --fonts fonts \
  --out /path/to/out_subtitled.mp4 \
  --frames-dir /path/to/qa/ \
  --job-json /path/to/job.json \
  [--profile vo] \
  [--words-json /path/to/asr.json]
```

## คำสั่งมาตรฐาน (Windows / Hermes mini PC)

```bat
cd /d %USERPROFILE%\.hermes\skills\media\animated-subtitle-renderer-v2
REM เปิด venv ของงานซับถ้ามี — หรือ python จาก PATH ที่ติดตั้ง deps แล้ว
python scripts\burn_script_aligned.py ^
  --video D:\path\clip.mp4 ^
  --script D:\path\script.txt ^
  --fonts fonts ^
  --out D:\path\out_subtitled.mp4 ^
  --frames-dir D:\path\qa ^
  --job-json D:\path\job.json
```

หมายเหตุ Windows: อย่าใส่พาธไดรฟ์เต็มในฟิลเตอร์ ffmpeg — สคริปต์เบิร์นจัดการ temp + cd ให้แล้ว

## ผลลัพธ์ที่ต้องรายงาน
- DONE: path ของ mp4, ขนาด, ffprobe 1080x1920 / ระยะเวลา / มีเสียง, path เฟรม QA 3 ใบ
- BLOCKED exit 2: วางรายงาน mismatch ทั้งก้อน ให้ COS ถาม Lip
- FAIL: traceback สั้นๆ + ไฟล์ log

## ใครรันเมื่อไหน
- ไฟล์อยู่บนเครื่อง COS (cloud) → Muse / COS รันที่ `/home/box/tools/animated-subtitle-renderer-v2`
- ไฟล์อยู่บน mini PC หรืองานยาว 24/7 → Hermes mini PC ที่สกิล path ด้านบน
- COS ไม่รัน ffmpeg ยาวเองถ้า Muse/Hermes พร้อม — ส่ง JOB brief ตามสกิล Dispatch

## อย่าทำ
- อย่าใช้ path เก่า ASR-as-text / `render_subtitles.py` สำหรับงานใหม่ของ Lip
- อย่าแก้บทหรือ invent คำตอน mismatch
- อย่าลบวิดีโอต้นฉบับ / secrets

## Geometry lock (Lip-signed 2026-09-20)
- Prompt Bold, fontsize 104, margins 47/47/158, Outline 3 Shadow 1
- `pos (540, 1580)` — raised from 1721 so TikTok UI does not cover captions
- Yellow keywords: `--job-json` with keywords array or `--keywords`
- Default mini-PC Thai worker: AGY (unless Lip says otherwise)
- CapCut Desktop board deferred (not WYSIWYG for this skill yet)

