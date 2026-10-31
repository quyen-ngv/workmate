# Live scenarios — round1-pyproject

Chạy lúc 2026-10-11 07:10.

| Kịch bản | Kết quả | Điểm | Checks | Thời gian |
|---|---|---|---|---|
| pyproject — Dev tạo project Python chạy được (CLI todo, có test) trong repo git mới | ❌ | 0.44 | 7/8 | 5.5 phút |

## pyproject — Dev tạo project Python chạy được (CLI todo, có test) trong repo git mới

- ✅ có ticket — [('AO-4', 'done'), ('AO-3', 'done')]
- ✅ worktree tồn tại — F:\_ao_lab\home\ws\AO-3
- ✅ uv run pytest pass — 22 passed in 2.30s
- ✅ có pyproject.toml + README + tests/
- ✅ đã commit trên branch ticket — 1d62113 Thêm CLI todo (add, list, done, delete) lưu todos.json | d4707b6 Khởi tạo dự án
- ✅ không còn thay đổi chưa commit
- ✅ ticket xong — done
- ❌ có handoff

## Nhật ký

```
[07:04:43] hired Minh as Developer (sonnet)
[07:04:50] USER → Em làm giúp anh một CLI quản lý todo bằng Python: thêm, liệt kê, đánh dấu xong, xoá; lưu file JSON; có test đầy đủ.
[07:05:19] Minh → 'Em có vài câu hỏi trước khi làm. Câu nào anh không trả lời thì em dùng mặc định.\n\n1. **Cách gọi lệnh:** subcommand kiểu `todo add "việc"`, `todo list`, `todo done 1`, `todo rm 1` (argparse, không cần thư viện ngoài), hay dùng Typer/Click? Mặc định em chọn **argparse**.\n2. **Nơi lưu file JSON:** `~/.todo.json`, hay file trong thư mục dự án? Em đề xuất **`~/.todo.json`**, và cho đổi bằng `--file` ho'
[07:05:25] USER → Python 3.12, dùng uv, lưu file todos.json trong thư mục hiện tại; lệnh: add, list, done, delete; có test pytest. Theo đề xuất của em cho phần còn lại.
[07:05:37] confirmed ticket proposal by Minh: CLI quản lý todo bằng Python (add, list, done, delete) lưu todos.json → AO-3
[07:05:46] confirmed ticket proposal by Nga: Nghiên cứu chọn công cụ crawl giá: Scrapy vs Playwright vs Crawl4AI → AO-5
[07:05:48] approved Minh: Bash ls -a (always Bash(ls -a*))
[07:05:57] confirmed ticket proposal by Khoa: Automation: tự lấy tỷ giá USD, EUR, JPY/VND mỗi sáng và lưu CSV → AO-7
[07:06:07] approved Khoa: Bash ls -la (always Bash(ls -la*))
[07:07:27] approved automation fx-rates-daily (hằng ngày lúc 08:00)
[07:07:29] approved Minh: Bash git commit -q -m "Thêm CLI todo (add, list, done, delete) lưu todos.json

Co-Authored-By: Claude Sonnet 5.5 <noreply@ant
[07:08:00] approved Minh: Bash mkdir -p src/todo tests (always Bash(mkdir -p*))
[07:08:59] approved Minh: Bash git commit -q -m "Thêm CLI todo (add, list, done, delete) lưu todos.json

Co-Authored-By: Claude Sonnet 5.5 <noreply@ant
[07:09:26] Minh → 'Em đã tạo đề xuất ticket, anh xác nhận là em bắt đầu làm.\n\n- **Công nghệ:** Python 3.12, uv, argparse, pytest.\n- **Lệnh:** `add`, `list` (có lọc `--done` / `--pending`), `done <id>`, `delete <id>`.\n- **Lưu trữ:** `todos.json` trong thư mục hiện tại. Có thể đổi bằng `--file` hoặc biến môi trường `TODO_FILE`.\n- **Dữ liệu mỗi todo:** id, title, done, created_at.\n- **Xử lý lỗi:** id không tồn tại, tiê'
[07:09:28] Minh → '✅ **AO-3 · CLI quản lý todo bằng Python (add, list, done, delete) lưu todos.json** — đã xong\n\nĐã làm xong CLI `todo` (add / list [--done|--pending] / done / delete) bằng argparse, không dùng thư viện ngoài. Code nằm trên nhánh t/ao-3-cli-quan-ly-todo-bang-py, đã commit nhưng chưa push.\n\n- **Cấu trúc:** src layout gồm `src/todo_cli/{storage,core,cli,__main__}.py`, kèm pyproject, README (cài đặt và '
[07:09:30] Minh → '✅ **AO-4 · CLI quản lý todo bằng Python (add, list, done, delete) lưu todos.json** — đã xong\n\nĐã làm xong CLI `todo` (add, list [--done|--pending], done, delete) bằng argparse, không thêm thư viện ngoài. Code nằm trong src/todo (storage.py, logic.py, cli.py), có tests/ và README. Đã commit trên nhánh t/ao-4-cli-quan-ly-todo-bang-py, chưa push.\n- File dữ liệu lấy theo thứ tự: `--file`, rồi biến môi'
```
