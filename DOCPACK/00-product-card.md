---
title: Book Opportunity Intelligence — Product Card
doc_id: CARD-book-opportunity-intelligence
doc_type: product-card
verdict: showcase
version: 0.1
date: 2026-10-08
status: ร่างเพื่อพิจารณา
owner: Product owner (Book Dev)
---

# Book Opportunity Intelligence — Product Card

## 1. สรุปคุณค่าในหนึ่งประโยค

อ่าน envelope จาก data product 7 โดเมน (crypto, stock, FX, DeFi, flight, SEO, AI tools) แล้ว normalize, dedupe, ให้คะแนน และจัดอันดับเป็น 'โอกาส' ใน Gold layer พร้อม API อ่านอย่างเดียว

## 2. ปัญหาและผู้ใช้เป้าหมาย

- ข้อมูลจากหลายโดเมนแยกกัน เจ้าของต้องการมุมมองเดียวว่าอะไรน่าสนใจ
- ผู้ใช้: เจ้าของ (เครื่องมือส่วนตัว) ไม่มีผู้ใช้ภายนอก

## 3. สิ่งที่ทำได้จริงตอนนี้

- `synthesis.py`: normalize, fuzzy dedupe (รองรับ n-gram ภาษาไทย), categorize, `score_opportunity` และ rank
- `gold.py` เขียน Gold Parquet + synthesis manifest พร้อม input lineage; `api.py` เสิร์ฟ `opportunity.v1` บน port 8108
- fallback เป็น fixture ต่อโดเมนเมื่อ API ล่ม และบันทึกสถานะรายโดเมน; ไม่ scrape ต้นทางเอง
- test 10 ไฟล์ รวม integration smoke

## 4. หลักฐานสถานะ

- Stack: Python
- Commits: 13 (แรก 2026-08-11 → ล่าสุด 2026-10-01), branch `main`; working tree สะอาด
- Source: 22 ไฟล์ / 7,101 บรรทัด; test 10 ไฟล์
- CI: 1 workflow
- Deploy config: ไม่มี
- Live URL: ไม่พบใน repo

## 5. คุณค่าและจุดต่าง

- ออกแบบ lineage และ contract ดี เป็นตัวอย่าง consumer ของ lake
- การให้คะแนนเป็น heuristic ตามคำสำคัญ (+10 ถ้ามีคำว่า 'ai', +5 ถ้ามีคำเกี่ยวกับเงิน) ไม่ได้วัดมูลค่าโอกาสจริง
- มุมรายได้: ยังไม่มี **[สมมติฐาน: อาจเป็นจดหมายข่าวโอกาส]**

## 6. ช่องว่างสำคัญ

- ไม่มีหลักฐานว่าคะแนนช่วยตัดสินใจได้ดีขึ้น
- ไม่ได้รวมตระกูล intake (ทุน, จัดซื้อ, โปรแกรมสตาร์ทอัพ) ที่น่าจะเป็น 'โอกาส' จริงกว่า
- ไม่มี deploy หรือหน้าจอของตัวเอง (หน้าจอคือ `solo-empire-insights`)

## 7. คำตัดสิน

**showcase** — สถาปัตยกรรมดีและทำงานจริง แต่คุณค่าทางธุรกิจยังไม่พิสูจน์เพราะคะแนนเป็น heuristic

จะเป็น `invest` เมื่อเจ้าของใช้รายการนี้ตัดสินใจจริงและมีการวัดผล หรือเมื่อรวม intake family แล้วกลายเป็นบริการแจ้งเตือนโอกาสที่มีผู้ใช้

## 8. ขั้นต่อไป

1. ทดลองเพิ่ม input จากตระกูล intake (`book-scholarship-data` ฯลฯ)
2. ออกแบบเกณฑ์คะแนนที่วัดผลได้แทน keyword boost

## การอนุมัติ

| บทบาท | สถานะ | วันที่ | ลงนาม |
|---|---|---|---|
| Product owner (Book Dev) | รอพิจารณา | — | ________ |

## แหล่งข้อมูล

- `README.md`
- `src/book_opportunities/synthesis.py`
- `src/book_opportunities/api.py`
- `src/book_opportunities/gold.py`
- `tests/`
