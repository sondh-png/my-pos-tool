# Mô tả dự án: Tool học và phân tích lỗi API GHN

## Mục tiêu

Xây dựng một công cụ nội bộ có khả năng **tự học, ghi nhớ và phân tích lỗi phát sinh khi làm việc với API GHN (`api.ghn.vn`)**, từ đó hỗ trợ việc debug, hướng dẫn sửa lỗi và đề xuất đoạn code chính xác.

---

## Bối cảnh

Hiện tại tôi đã có:

- Một **nền tảng giả lập quản lý đơn hàng** chạy trên máy tính
- Một **group Telegram lưu trữ lịch sử xử lý lỗi**
- Dữ liệu trao đổi thực tế về:
    - lỗi API
    - nguyên nhân
    - cách khắc phục
    - ví dụ code sửa lỗi

---

## Mục đích của tool

Tool cần có khả năng:

### 1. Học từ tài liệu API GHN

Tool đọc và hiểu:

- endpoint
- request body
- response
- required field
- validation rule
- authentication
- error response

Ví dụ:

```
tenant_masterdata_get_shift_detail→ bắt buộc có ShiftID
```

---

### 2. Học từ group Telegram

Tool tự động đọc lịch sử chat trong group Telegram để:

- phát hiện lỗi
- trích xuất nguyên nhân
- lưu cách xử lý
- ghi nhớ solution đã từng xử lý

Ví dụ:

```
Field validation for 'ShiftID' failed
```

Tool ghi nhớ:

```
Nguyên nhân:Thiếu ShiftIDCách sửa:Thêm myRequest.ShiftID vào payload
```

---

### 3. Kết nối trực tiếp API GHN

Tool có thể:

- gửi request test
- nhận response
- bắt lỗi
- phân tích lỗi phát sinh
- ghi log vào database

---

### 4. Ghi nhớ lỗi

Mỗi lỗi phải được lưu lại với đầy đủ thông tin:

- endpoint
- message lỗi
- nguyên nhân
- cách sửa
- code đúng
- ví dụ thực tế
- nguồn học (Telegram / API docs / log hệ thống)

---

### 5. Hỏi đáp và tư vấn sửa lỗi

Khi tôi hỏi:

> lỗi này là gì?

hoặc

> tại sao API báo thiếu ShiftID?

Tool phải trả lời được:

- lỗi nằm ở đâu
- nguyên nhân
- field nào sai
- đoạn code sai
- đoạn code đúng
- hướng xử lý

Ví dụ:

**Input:**

```
Field validation for 'ShiftID' failed
```

**Output mong muốn:**

```
Nguyên nhân:Payload thiếu ShiftIDSai ở:myRequestCode đúng:{   "ShiftID": 123}Cách xử lý:Kiểm tra field bắt buộc trước khi call API
```

---

## Mục tiêu cuối cùng

Tạo ra một **AI assistant nội bộ chuyên về API GHN**, có khả năng:

- học từ lỗi cũ
- học từ log thực tế
- tự tích lũy kinh nghiệm
- hỗ trợ debug
- hướng dẫn code đúng
- giảm thời gian xử lý lỗi cho team

---

## Nguồn dữ liệu học

### Nguồn 1

API docs GHN  
`api.ghn.vn`

### Nguồn 2

Lịch sử lỗi từ group Telegram

### Nguồn 3

Log lỗi từ nền tảng giả lập quản lý đơn hàng

---

## Công nghệ dự kiến

- Python
- Telegram API
- SQLite
- FastAPI
- Error Analyzer
- Knowledge Base