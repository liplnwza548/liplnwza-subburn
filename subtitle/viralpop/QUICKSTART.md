# QUICKSTART — script-aligned burn (v1 DEFAULT, Linux/COS/Muse)

> DEFAULT: script = TEXT, STT = TIMING, mismatch gate before burn.
> The old ASR-as-text path below (§4 Windows build_clip flow using
> STT transcript as cue text) is DEPRECATED for COS/Muse — it caused
> the primary-case failures (มาก leak, คลาสสิก split, อย่าบ spelling).
> Use this section for all new jobs.

```bash
source /home/box/subtitle-work/activate.sh
cd /home/box/tools/animated-subtitle-renderer-v2

# 0. Script file: one cue per line, verbatim (Lip's wording, no fixes)
printf 'สวยแพงมาก\nทรงสวย\n' > /home/box/subtitle-work/jobs/bag_demo.txt

# 1. Burn (live Groq STT for timing; key in /home/box/subtitle-work/.env)
python scripts/burn_script_aligned.py \
  --video /home/box/subtitle-work/clips/<clip>.mp4 \
  --script /home/box/subtitle-work/jobs/bag_demo.txt \
  --fonts fonts --out /home/box/subtitle-work/output/<name>_subtitled.mp4

# ...or reuse cached Groq verbose_json (offline / tests, skips live STT):
python scripts/burn_script_aligned.py \
  --video <clip>.mp4 --script <script>.txt \
  --fonts fonts --out <out>.mp4 --words-json asr_test/<name>.json
```

- Exit 0 = burned + QA (ffprobe geometry/duration/audio + 3 frames).
- Exit 2 = mismatch gate BLOCKED: report lists ALL bad lines at once
  (bad line + time window + heard snippet each). No ASS/MP4 written.
  Fix the script lines (or audio), re-run. Ask Lip once, not per line.
- Smoke: `python3 tests/test_primary_bag_demo.py` (must stay green).

---

# QUICKSTART (legacy ASR-as-text — DEPRECATED for COS/Muse)

ทุกคำสั่งรันจากโฟลเดอร์งาน (เช่น `subtitled\`). สกิลอยู่ที่
`animated-subtitle-renderer-v2\`. ไฟล์เสียง/transcript/output อยู่นอกสกิล

## 0. ครั้งแรกครั้งเดียว

```powershell
powershell -ExecutionPolicy Bypass -File animated-subtitle-renderer-v2\setup.ps1
Copy-Item .env.example .env   # แล้วใส่ GROQ_API_KEY=gsk_... ใน .env
```

ห้าม commit / zip / อัปโหลด `.env` เด็ดขาด (มีคีย์จริง)

## 1. ถอดเสียง (ขอ word timestamps)

```powershell
ffmpeg -y -i <clip>.mp4 -vn -ac 1 -ar 16000 -c:a pcm_s16le <clip>_16k.wav
python animated-subtitle-renderer-v2\render\transcribe_groq.py <clip>_16k.wav asr_test\<name>.json
```

ต้องได้ `SEGMENTS n WORDS m` (m > 0) ถ้าเจอ `WORD_TIMESTAMPS_MISSING`
คือ Groq รอบนั้นไม่ให้เวลาระดับคำ — ห้ามทำต่อ ให้รันใหม่

## 2. เขียน job config (`jobs\<name>.json`)

ลอก `jobs\metal_card_holder.json` แล้วแก้:

- `segments`: จำนวนต้องเท่ากับ transcript (เช็ค `id` ให้ตรง) ใส่บทที่แก้สะกดแล้ว
  (ไม่เปลี่ยนคำพูด) ช่องละ 1 วลีก็พอ packer จัด 3–5 พยางค์ให้เอง
- `keywords`: คำที่อยากให้เป็นสีเหลือง (ต้องสะกดตามบทที่แก้แล้ว)
- `font_file/playres/fontsize/margins/pos`: ปล่อย `"auto"` ได้ (อ่านจากวิดีโอ)
- คำทับศัพท์คำใหม่: เพิ่มลง `loanword_dict.json` ก่อน (ดู `_note` ในไฟล์)

## 3. Build ASS

```powershell
python animated-subtitle-renderer-v2\render\build_clip.py --config jobs\<name>.json --transcript asr_test\<name>.json --ass output_<name>\<name>.ass --json output_<name>\<name>_transcript.json --video <clip>.mp4
```

ต้องได้ `match ratio` ใกล้ 1.0 ทุก cue, `FORCED_WIDTH_SPLITS 0`
(ถ้าไม่ใช่ 0 เปิด transcript json ดู cue ที่ `forced_split: true` แล้วรีวิว)

## 4. Render + QA

```powershell
python animated-subtitle-renderer-v2\render\render_subtitles.py <clip>.mp4 output_<name>\<name>.ass output_<name>\<name>_subtitled.mp4
ffmpeg -ss <t> -i output_<name>\<name>_subtitled.mp4 -frames:v 1 qa.png
```

เช็ค: ความยาว/ขนาด/เสียงตรงต้นฉบับ, ซับตรงปาก ≥5 จุด, ไม่ล้นขอบ,
keyword เหลืองขนาดเท่าเดิม, 1 cue = 1 บรรทัด (ไม่มี `\N`)

## เทสทั้งระบบ

```powershell
python tests\test_skill.py          # unit + golden + failure modes
```

## ถ้าพัง อ่านอะไรก่อน

- เวลาไม่ตรง → `asr_test\<name>.json` มี `words[]` ไหม (gate ข้อ 1)
- ตัวอักษรเพี้ยน/สะกดแปรผัน → `loanword_dict.json` ขาด variant
- สระซ้อนกัน → `references\THAI_SHAPING_ROOT_CAUSE.md`
- หมายเหตุฟิลเตอร์: default `subtitles=` ผ่าน QA แล้ว อย่าเปลี่ยนเป็น
  `--filter ass` ถ้ายังไม่ได้รัน A/B บนเครื่องนั้น
