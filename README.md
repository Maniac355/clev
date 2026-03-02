# clevai-form-bot

Bot Python 1-file để:
1. Fetch dữ liệu từ API Clevai.
2. Lọc theo `teacher_status` sau khi fetch.
3. Submit Google Form bằng Playwright.

File chạy chính: `clevai_form_bot.py`

## 1. Clone repo

```bash
git clone https://github.com/Maniac355/clev.git
cd clev
```

## 2. Cài đặt nhanh (Windows)

Cách nhanh nhất:
1. Chạy `INSTALL_WINDOWS.bat`
2. Chờ script cài xong toàn bộ dependencies + Playwright Chrome

Hoặc tự cài bằng lệnh:

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chrome
```

## 3. Cấu hình dữ liệu cần thiết

Sao chép file `.env.example` thành `.env`.

Các trường quan trọng:
1. `CLEVAI_API_URL`: để nguyên `https://api.clevai.edu.vn/api/v1/so/meeting/search`
2. `GOOGLE_FORM_URL`: link form cần submit
   Mặc định hiện tại:
   `https://docs.google.com/forms/d/e/1FAIpQLSczXsxPX08gQt9Z9e-_-mJwvM8rcMbdHgtgI4EHkSV_aJ2IQQ/viewform`
3. `CLEVAI_BEARER_TOKEN`: token API (có thể để trống và nhập lúc chạy)
4. `BOT_PROFILE_DIR`: để trống để dùng profile riêng ổn định

## 4. Luồng chạy chuẩn nhất

```bash
python clevai_form_bot.py run
```

Bot sẽ hỏi lần lượt:
1. `SO` (Enter = mặc định `AnhNHT`)
2. `WHO`
3. `Token` (Enter để bỏ qua)
4. `teacher_status` (`0`, `1`, `3`, hoặc `0,3`)
5. Có submit luôn hay không (`y/n`)
6. `Note` (mặc định `kvl`)
7. Chọn profile theo số

Kết quả:
1. Dữ liệu fetch lưu vào `api_live_records.json` (ghi đè mỗi lần chạy).
2. Submit từng record lên form.
3. Hiển thị progress bar realtime và bảng tổng kết.

## 5. Các mode khác

```bash
python clevai_form_bot.py fetch   # Chỉ fetch API
python clevai_form_bot.py submit  # Chỉ submit từ file JSON
python clevai_form_bot.py login   # Mở browser để login profile Google
```

## 6. Mapping field lên form

1. `Mã lớp` -> `clag_code`
2. `Mã GV MAIN` -> `gte_usi`
3. `SĐT GV` -> `gte_phone`
4. `Note` -> `kvl` (hoặc giá trị nhập)

## 7. teacher_status

1. `0` -> `ABSENCE`
2. `1` -> `ATTEND`
3. `3` -> `QUIT_EARLY`

## 8. Chạy bằng click

1. `INSTALL_WINDOWS.bat` (cài lần đầu)
2. `clev.bat` (chạy hằng ngày)
