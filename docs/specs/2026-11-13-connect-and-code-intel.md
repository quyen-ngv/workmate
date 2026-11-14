# Kết nối & Hiểu code: Jira, Confluence, Bitbucket, GitHub, chỉ mục code, đồ thị code, bản đồ dịch vụ

Phiên bản: 1 · Cập nhật: 2026-11-13 · Trạng thái: thiết kế, chưa code
Liên quan: `docs/specs/2026-11-13-doc-core.md` (Doc Core dùng các cổng định nghĩa ở đây)

## 1. Mục tiêu và phạm vi

| # | Cần có | Mục |
|---|---|---|
| G-1 | Truy cập code trên **Bitbucket** và **GitHub**, AI dùng được qua MCP | 5, 9 |
| G-2 | **Chỉ mục code** và **đồ thị code** để AI đọc code nhanh, ít token, có trích dẫn | 6, 7 |
| G-3 | **Bản đồ dịch vụ**: hình hoá quan hệ giữa các service | 8 |
| G-4 | Kết nối **Jira Cloud**, **Confluence Cloud** để lấy tài liệu | 4 |
| G-5 | Mọi kết nối **có sẵn trong sản phẩm dù chưa cấu hình**; cần thì bật, không phải code thêm | 3 |
| G-6 | Cùng một kiến trúc adapter cho mọi thứ; thêm hệ thống mới không sửa lõi | 2 |

Không thuộc phạm vi bản này: Bitbucket Data Center, GitLab, Azure DevOps, SharePoint (đã có chỗ cắm, chưa viết adapter);
đồng bộ ticket hai chiều với Jira (hiện chỉ mirror một chiều, giữ nguyên).

Thuật ngữ: **kết nối** = một tài khoản tới một hệ thống ngoài. **Năng lực** = một việc kết nối làm được (đọc tài liệu,
đọc code, tạo PR…). **Chỉ mục** = dữ liệu office tự dựng từ code để tìm nhanh. **Đồ thị** = các nút (service, file,
hàm, endpoint, topic, bảng) và cạnh giữa chúng.

## 2. Kiến trúc tổng thể

```
 ┌──────────────────────────── Người dùng ────────────────────────────┐
 │  Web: Cài đặt › Kết nối      Web: Bản đồ dịch vụ      Thẻ duyệt     │
 └───────────────┬──────────────────────┬─────────────────────────────┘
                 │                      │
 ┌───────────────▼──────────────────────▼─────────────────────────────┐
 │ Tool cho AI (một bộ tên, mọi runtime, phục vụ qua MCP `office`)     │
 │   source_get · source_search · code_search · code_outline ·         │
 │   code_symbol · code_refs · code_read · code_impact · service_map   │
 └───────────────┬──────────────────────┬─────────────────────────────┘
                 │                      │
 ┌───────────────▼─────────┐  ┌─────────▼──────────────────────────────┐
 │ ConnectionService       │  │ CodeIntelService                       │
 │ kết nối, bí mật, sức    │  │ đồng bộ repo → chỉ mục → đồ thị →      │
 │ khoẻ, hạn mức, cache    │  │ truy vấn → bản đồ dịch vụ              │
 └───────────────┬─────────┘  └─────────┬──────────────────────────────┘
                 │ dùng cổng            │ dùng cổng CodeHost để lấy mã
 ┌───────────────▼──────────────────────▼─────────────────────────────┐
 │ Cổng (Protocol):  DocSource · Tracker · Wiki · CodeHost · Extractor │
 ├─────────────────────────────────────────────────────────────────────┤
 │ Adapter:  jira_cloud · confluence_cloud · bitbucket_cloud · github  │
 │           + http / mcp / command (tổng quát) + plugin (entry point) │
 └─────────────────────────────────────────────────────────────────────┘
```

Ba nguyên tắc:

1. **Office tự gọi hệ thống ngoài bằng adapter API; AI gọi office bằng MCP.** Ghim bằng chứng, dựng chỉ mục, kiểm độ
   tươi là việc của office, phải xác định và lặp lại được, không qua mô hình. AI chỉ thấy bộ tool thống nhất ở trên, nên
   đổi Bitbucket sang GitHub thì prompt, luật và tài liệu không đổi. Chi tiết về MCP ở mục 9.
2. **Một registry, một kiểu cấu hình** cho mọi adapter (`use: <tên>` + tham số), đăng ký qua entry point, đúng cơ chế đã
   nêu ở Doc Core 7.6. Adapter có sẵn và plugin đi cùng một đường.
3. **Chưa cấu hình là một trạng thái hợp lệ.** Mọi tính năng phụ thuộc kết nối phải chạy được ở mức thấp hơn và nói rõ
   thiếu gì (mục 3.4).

### 2.1 Cấu trúc thư mục

```
src/actoroffice/
  ports/                      # Protocol + DTO, thuần (stdlib + pydantic)
    connection.py             ConnectionSpec, Capability, Health
    docs.py                   DocSource, Tracker, Wiki          (DocSource = SourceProvider của Doc Core)
    code.py                   CodeHost, CodeReader, RepoRef, PullRequest
    intel.py                  CodeIntel (truy vấn), Extractor (plugin trích quan hệ)
  adapters/
    registry.py               nạp entry point, dựng adapter từ cấu hình, kiểm config_schema
    atlassian/auth.py         xác thực + phát hiện cloudId dùng chung
    atlassian/jira_cloud.py   Tracker + DocSource
    atlassian/confluence_cloud.py   Wiki + DocSource
    atlassian/adf.py · storage_html.py     chuyển ADF / storage format → markdown
    bitbucket_cloud.py        CodeHost
    github.py                 CodeHost
    generic/http.py · mcp.py · command.py
  codeintel/
    sync.py                   mirror repo, phát hiện thay đổi
    index.py                  manifest, chunk, FTS
    parse.py                  tree-sitter → ký hiệu, import, lời gọi
    graph.py                  nút, cạnh, phân giải tham chiếu
    extractors/               spring.py · mybatis.py · openapi.py · kafka.py · http_client.py · deploy.py · build_deps.py
    services.py               phát hiện service, nối cạnh liên service
    query.py                  hiện thực CodeIntel
    pack.py                   context_pack: gói đọc theo ngân sách token
  services/connections.py     ConnectionService
  services/codeintel.py       CodeIntelService (lập lịch, trạng thái, sự kiện)
  tools/source_tools.py · code_tools.py
  web/static/team/map.*       trang Bản đồ dịch vụ
```

Luật phụ thuộc (có test giữ): `ports/` không import gì của office; `adapters/` chỉ import `ports/`; `codeintel/` chỉ
import `ports/`; chỉ `services/*` nối chúng với db và engine.

## 3. Kết nối (G-5, G-6)

### 3.1 Mô hình

```python
class Capability(StrEnum):
    DOCS = "docs"            # đọc tài liệu theo tham chiếu, tìm kiếm
    TRACKER = "tracker"      # issue: đọc, tìm, bình luận
    WIKI = "wiki"            # trang: đọc, tìm, cây con, xuất bản
    CODE_READ = "code_read"  # liệt kê repo, đọc file, lấy mã về
    CODE_PR = "code_pr"      # tạo/đọc PR, bình luận, trạng thái commit
    WEBHOOK = "webhook"      # nhận sự kiện đẩy

class ConnectionSpec(BaseModel):
    id: str                  # "jira-main", "gh-acme"
    use: str                 # tên adapter: "jira_cloud", "github"…
    scope: str               # "office" | "team:<id>" | "project:<id>"
    config: dict[str, Any]   # theo config_schema của adapter; KHÔNG chứa bí mật
    secret_ref: str | None   # tên file trong <data_dir>/secrets/
    enabled: bool = True
    write: bool = False      # mặc định chỉ đọc; bật mới được bình luận, tạo PR, xuất bản

class Health(BaseModel):
    state: str               # unconfigured | ok | degraded | error
    checked_at: str
    detail: str              # tiếng Việt: thiếu gì, lỗi gì, cách sửa
    identity: str | None     # tài khoản mà token thuộc về
    scopes: list[str]        # quyền token thật sự có
    rate: dict[str, Any]     # hạn mức còn lại nếu hệ thống báo
```

Mỗi adapter khai:

```python
class Adapter(Protocol):
    name: str
    title: str                              # hiện trên web
    capabilities: set[Capability]
    config_schema: type[BaseModel]          # web tự dựng form, registry kiểm khi lưu
    secret_fields: list[str]                # ô nào là bí mật
    required_scopes: dict[Capability, list[str]]
    async def test(self) -> Health: ...     # gọi một API nhẹ, đọc quyền
    async def close(self) -> None: ...
```

### 3.2 Phân lớp và chọn kết nối

Kết nối khai ở office, team hoặc project. Khi cần một năng lực cho một ticket, `ConnectionService.resolve(capability,
scope)` lấy theo thứ tự **project → team → office**, cái đầu tiên `enabled` và có năng lực đó. Một năng lực có thể có
nhiều kết nối (hai tổ chức GitHub): khi đó chọn theo `match` của adapter trên tham chiếu (host, tên tổ chức, khoá dự án).

### 3.3 Bí mật và quyền

- Bí mật nằm ở `<data_dir>/secrets/<secret_ref>.json`, cùng chỗ và cùng cách bảo vệ như `jira.json`, `bitbucket.json`
  hiện nay (thư mục đã nằm trong `protected_dirs` của `PolicyEngine`). AI không bao giờ thấy token.
- `Redactor` đăng ký mọi giá trị bí mật khi nạp, để không lọt vào log, sự kiện hay prompt.
- Mặc định `write: false`. Hành động ghi (bình luận Jira, tạo PR, xuất bản trang) vẫn đi qua luật `external` hiện có.
- Adapter chỉ gọi tới host của chính kết nối đó. URL do người dùng hoặc tài liệu đưa vào không bao giờ được dùng làm đích
  gọi kèm token (chống SSRF và lộ token).
- Nội dung lấy từ ngoài được đánh dấu **không tin cậy** (cơ chế taint của `security/policy.py`): là dữ liệu để đọc, không
  phải chỉ dẫn cho AI.

### 3.4 Chưa cấu hình vẫn chạy

| Tình huống | Hành vi |
|---|---|
| Epic nhắc `PAY-120`, chưa có kết nối Jira | Nguồn hiện trạng thái "chưa kết nối Jira", có nút **Kết nối**. Check `sources_read` báo đúng lý do thay vì "chưa đọc" |
| Chưa có kết nối code host, project có `path` cục bộ | Chỉ mục và đồ thị dựng từ thư mục cục bộ. Không có PR, không webhook |
| Chưa có gì về code | `code_*` trả "project chưa có nguồn code", gợi ý ba cách: thư mục cục bộ, Bitbucket, GitHub |
| Token hết hạn giữa chừng | Kết nối sang `error`, thẻ báo cho admin của team; lượt chạy không vỡ, nguồn liên quan thành "không đọc được (lỗi)" |

Trang **Cài đặt › Kết nối** liệt kê **mọi** adapter đã đăng ký, kể cả chưa dùng, mỗi cái một dòng: trạng thái, phạm vi,
tài khoản, quyền thiếu, nút Kết nối / Kiểm tra / Tắt. Form sinh từ `config_schema`.

### 3.5 Hạn mức, cache, thử lại (dùng chung)

`adapters/registry.py` bọc mọi adapter HTTP bằng một lớp chung:
- Tôn trọng `429` + `Retry-After`; lùi theo cấp số nhân có nhiễu; tối đa 4 lần; giới hạn số yêu cầu đồng thời theo kết nối.
- Cache theo phiên bản: bảng `source_cache(connection_id, ref, version, etag, body, fetched_at)`. Yêu cầu lại kèm
  `If-None-Match` khi hệ thống hỗ trợ; nội dung không đổi thì không tốn hạn mức.
- Mọi lỗi thành `ConnectorError(kind)` với `kind` thuộc: `auth`, `forbidden`, `not_found`, `rate_limited`, `network`,
  `invalid`. Tool và web hiển thị theo `kind`, không in nguyên phản hồi của hệ thống ngoài.

## 4. Jira Cloud và Confluence Cloud (G-4)

### 4.1 Xác thực

| Cách | Khi nào | Ghi chú |
|---|---|---|
| Email + API token (Basic) | Mặc định. Đang dùng ở `services/jira.py`, `services/atlassian.py` | Một token dùng cho cả Jira và Confluence cùng site |
| OAuth 2.0 (3LO) | Đơn vị không cho dùng token cá nhân | Cần đăng ký app, lưu refresh token, gọi qua `api.atlassian.com/ex/<sản phẩm>/<cloudId>` |

`atlassian/auth.py` giấu khác biệt này: adapter chỉ nhận một `httpx.AsyncClient` đã có xác thực và base URL đúng.
Cần xác minh lúc làm (tôi chưa kiểm với tài liệu Atlassian hiện hành): token có phạm vi (scoped token) dùng base URL
nào, và tên chính xác của các scope chỉ đọc.

Hai kết nối `jira_cloud` và `confluence_cloud` tách riêng nhưng có thể dùng chung `secret_ref`.

### 4.2 Cổng

```python
class DocRef(BaseModel):
    connection: str; ref: str; locator: str = ""      # ref: "PAY-120" | id trang | URL

class DocContent(BaseModel):
    title: str; text: str                 # markdown
    version: str                          # Jira: trường updated; Confluence: số phiên bản trang
    url: str
    links: list[DocRef] = []              # tài liệu nó trỏ tới (để đi tiếp, mục 4.5)
    attachments: list[Attachment] = []
    truncated: bool = False
    notes: list[str] = []                 # phần không đọc được: "sơ đồ draw.io không có chữ"

class DocSource(Protocol):                # chính là SourceProvider của Doc Core
    def recognize(self, text: str) -> list[DocRef]: ...
    async def fetch(self, ref: DocRef, scope: Scope) -> DocContent: ...
    async def search(self, query: str, scope: Scope, limit: int = 10) -> list[DocHit]: ...
    def in_scope(self, ref: DocRef, scope: Scope) -> bool: ...

class Tracker(Protocol):
    async def issue(self, key: str) -> Issue: ...
    async def search(self, jql: str, limit: int = 50) -> list[Issue]: ...
    async def children(self, key: str) -> list[Issue]: ...          # issue con của epic
    async def comment(self, key: str, text: str) -> None: ...       # cần write

class Wiki(Protocol):
    async def page(self, page_id: str) -> Page: ...
    async def resolve(self, url: str) -> str: ...                   # URL (kể cả link rút gọn /x/…) → id trang
    async def children(self, page_id: str) -> list[PageRef]: ...
    async def search(self, cql_or_text: str, space: str | None, limit: int = 10) -> list[PageRef]: ...
    async def upsert_page(self, space: str, title: str, body_md: str, parent_id: str | None) -> Page: ...   # cần write
```

`Wiki` và `CodeHost` hiện có trong `services/atlassian.py` được chuyển sang `ports/` và mở rộng; `Confluence`,
`BitbucketCloud`, `JiraClient` hiện có thành ruột của adapter tương ứng (không viết lại từ đầu).

### 4.3 Jira Cloud: lấy gì

| Thứ | Chi tiết |
|---|---|
| Nhận ra | Khoá `ABC-123` có tiền tố thuộc `projects` của kết nối; URL `…/browse/ABC-123` |
| Một issue | Tiêu đề, loại, trạng thái, mô tả (ADF → markdown, dùng lại `adf_to_text` và mở rộng cho bảng, khối code), tiêu chí chấp nhận nếu ở trường tuỳ biến (khai `fields.acceptance` trong cấu hình), bình luận (mới nhất trước, có giới hạn), tệp đính kèm, issue liên kết, issue con, remote link (thường là trang Confluence) |
| `version` | Trường `updated` |
| Tìm | JQL trong phạm vi `projects` của kết nối; tìm chữ thì dựng JQL `text ~` |
| Phạm vi | `projects: [PAY, CORE]`; issue ngoài danh sách là "ngoài phạm vi" như SI-39 |
| Theo dõi thay đổi | Mặc định hỏi định kỳ `updated >= <mốc>` cho các issue đang là nguồn của ticket mở. Có webhook thì dùng webhook |

### 4.4 Confluence Cloud: lấy gì

| Thứ | Chi tiết |
|---|---|
| Nhận ra | URL trang của site kết nối: dạng `/wiki/spaces/<S>/pages/<id>/…`, `/wiki/x/<mã>`, `viewpage.action?pageId=` |
| Một trang | Storage format → markdown bằng `storage_html.py`: heading, danh sách, bảng (kể cả ô gộp → ghi chú), khối code, panel info/warning, link trang khác (thành `links`), macro Jira issue (thành `links` tới Jira), macro include/excerpt (nhúng nội dung, tối đa 2 cấp) |
| Không đọc được | Sơ đồ draw.io, Gliffy, ảnh: ghi vào `notes` kèm tên tệp. Người duyệt thấy "trang có 2 sơ đồ AI không đọc được" thay vì im lặng bỏ qua |
| Tệp đính kèm | pdf, docx, xlsx → chữ qua bộ trích dùng chung với adapter `upload` (Doc Core 7.1); có giới hạn kích thước |
| `version` | Số phiên bản trang |
| Cây trang | `children` để BA đưa một trang gốc và chọn "kèm trang con" |
| Phạm vi | `spaces: [PAY, ARCH]`; ngoài danh sách là "ngoài phạm vi" |
| Tìm | CQL giới hạn theo `spaces`; hoặc tìm trong bản sao cục bộ (4.6) |
| Xuất bản | `upsert_page`: markdown → storage format; dùng cho Publisher của Doc Core |

### 4.5 Đi theo liên kết

Một issue Jira thường trỏ tới trang Confluence, trang đó trỏ tiếp trang khác. `fetch` trả `links`; DocService thêm chúng
vào danh sách nguồn với `depth + 1`. Giới hạn trong cấu hình: `follow_links: {depth: 1, max: 20}`. Nguồn thêm theo cách
này mang nhãn "từ PAY-120" và mặc định **không bắt buộc đọc**; AI hoặc BA nâng lên bắt buộc nếu liên quan. Như vậy không
bỏ sót tài liệu liên quan mà cũng không bắt đọc cả wiki.

### 4.6 Bản sao để tìm kiếm (tuỳ chọn)

Mặc định lấy theo yêu cầu. Bật `mirror: true` cho một space thì office tải các trang về `source_cache`, đưa vào FTS5
(cùng cơ chế chỉ mục chữ ở mục 6.3), cập nhật theo `version`. Khi đó `source_search` tìm cục bộ, nhanh và không tốn hạn
mức. Chỉ lưu space trong `spaces`; xoá kết nối thì xoá bản sao.

## 5. Code host: Bitbucket Cloud và GitHub (G-1)

### 5.1 Cổng

```python
class RepoRef(BaseModel):
    connection: str; slug: str             # "acme/payments"
    default_branch: str = "main"

class CodeHost(Protocol):
    # --- đọc
    async def repos(self, query: str | None = None) -> list[RepoRef]: ...
    async def head(self, repo: RepoRef, branch: str | None = None) -> str: ...
    async def tree(self, repo: RepoRef, at: str) -> list[TreeEntry]: ...                 # path, blob sha, size
    async def file(self, repo: RepoRef, path: str, at: str) -> bytes: ...
    async def compare(self, repo: RepoRef, base: str, head: str) -> list[ChangedFile]: ...
    async def clone_auth(self, repo: RepoRef) -> CloneAuth | None: ...                   # URL + header/askpass tạm; None nếu cấm clone
    # --- PR (cần write, trừ đọc)
    async def pr_create(self, repo, source, destination, title, body) -> PullRequest: ...
    async def pr_get(self, repo, pr_id) -> PullRequest: ...
    async def pr_comments(self, repo, pr_id) -> list[PrComment]: ...
    async def pr_reply(self, repo, pr_id, text, parent_id=None) -> None: ...
    def parse_pr_url(self, url: str) -> tuple[RepoRef, int] | None: ...
    # --- sự kiện
    def verify_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent | None: ...
```

Sửa các chỗ đang gắn cứng Bitbucket: `parse_pr` (`tools/atlassian_tools.py:117`) thành `parse_pr_url` của từng adapter;
`services/prwatch.py` và `services/merges.py` lấy code host qua `ConnectionService.resolve(CODE_PR, scope)` thay vì
`office.atlassian.code_host()`; chuỗi `by="bitbucket"` thành tên kết nối.

### 5.2 Hai adapter

| | `bitbucket_cloud` | `github` |
|---|---|---|
| Xác thực mặc định | Email + API token (đang dùng) | Token cá nhân loại fine-grained |
| Xác thực cho tổ chức | Access token của workspace/repository | GitHub App (token cài đặt, tự làm mới) |
| GitHub Enterprise Server | — | Cùng adapter, đổi `base_url` |
| Lấy mã về | `git` qua HTTPS với token | `git` qua HTTPS với token |
| Sự kiện | Webhook `repo:push`, `pullrequest:*` | Webhook `push`, `pull_request`, `pull_request_review` |
| Quyền tối thiểu để đọc | repository: read, pullrequest: read | Contents: read, Pull requests: read, Metadata: read |
| Thêm để ghi | pullrequest: write | Pull requests: write |

Tên scope chính xác phải đối chiếu với tài liệu của từng hãng lúc làm; `required_scopes` của adapter là nơi ghi, và
`test()` so với quyền token thật có để báo thiếu gì.

### 5.3 Ba cách có mã để dựng chỉ mục

`CodeIntelService` chọn theo thứ tự, cái đầu dùng được:

| Cách | Điều kiện | Tốc độ | Đầy đủ |
|---|---|---|---|
| **Thư mục cục bộ** | `Project.path` có sẵn (như hiện nay) | Nhanh nhất | Đủ |
| **Mirror** | `clone_auth` trả về được | Nhanh; `git fetch` chỉ lấy phần mới | Đủ, có lịch sử |
| **Qua API** | Đơn vị cấm clone | Chậm, tốn hạn mức; chỉ nhánh chính | Đủ file, không lịch sử |

Mirror là bare repo ở `<data_dir>/repos/<connection>/<slug>.git`, chỉ đọc, tách hẳn khỏi worktree làm việc của ticket
(`engine/workspace.py` không đổi). Token đưa cho `git` qua biến môi trường tạm của tiến trình con, không ghi vào
`.git/config`.

### 5.4 Webhook

Một endpoint: `POST /hooks/<connection_id>`. Adapter kiểm chữ ký (`verify_webhook`) rồi trả `WebhookEvent(kind, repo,
ref, commit, pr)`. Sự kiện `push` lên nhánh chính → xếp hàng đồng bộ chỉ mục; sự kiện PR → thay cho việc hỏi định kỳ của
`PullRequestWatcher`. Không có webhook (office chạy sau tường lửa) thì hỏi định kỳ như hiện nay; cấu hình
`poll_s` theo kết nối.

## 6. Chỉ mục code (G-2)

### 6.1 Đường ống

```
đồng bộ ──▶ manifest ──▶ phân tích ──▶ đồ thị ──▶ trích quan hệ ──▶ nối service
(commit)   (file nào   (tree-sitter:  (nút, cạnh, (endpoint, topic,  (cạnh giữa
            đổi)        ký hiệu,       phân giải)   bảng, client)     các repo)
                        import, gọi)
```

Mỗi bước chỉ làm trên **file đổi** (so `blob sha` với manifest). Repo 5 000 file, push đổi 12 file thì phân tích 12 file
và phân giải lại các cạnh chạm tới chúng.

### 6.2 Manifest

Mỗi file: `path`, `blob_sha`, `lang`, `size`, `kind` (`source` | `test` | `config` | `doc` | `generated` | `vendored`).
Bỏ qua theo `.gitignore`, thêm `.officeignore`, file nhị phân, file lớn hơn `max_file_kb`, thư mục sinh ra
(`target/`, `build/`, `node_modules/`, `dist/`). `kind` quyết định có phân tích và có xếp hạng thấp hơn không.

### 6.3 Chỉ mục chữ

Cắt file theo ranh giới ký hiệu (một hàm, một lớp nhỏ, một khối cấu hình), không cắt theo số dòng cố định. Mỗi đoạn vào
bảng FTS5 (đã dùng ở `0003_memory.sql`, `0013_knowledge.sql`) với tokenizer `trigram`, để tìm được một phần của tên định
danh (`LimitSvc` tìm ra `DailyLimitServiceImpl`) và chuỗi có ký tự đặc biệt.

### 6.4 Chỉ mục ký hiệu

`codeintel/parse.py` dùng tree-sitter. Mỗi ngôn ngữ một file truy vấn (`queries/<lang>.scm`) khai: định nghĩa (lớp,
interface, hàm, phương thức, trường, hằng), import, lời gọi, kế thừa, annotation/decorator. Thêm ngôn ngữ = thêm một
file truy vấn, không sửa code.

Thứ tự hỗ trợ: **Java, Kotlin, TypeScript/JavaScript, Python** trước; sau đó Go, C#, Dart. Ngôn ngữ chưa có truy vấn vẫn
có chỉ mục chữ và manifest.

Phụ thuộc mới: `tree-sitter` và gói grammar, đặt trong nhóm tuỳ chọn `[project.optional-dependencies] intel`. Không cài
thì office vẫn chạy, chỉ mục dừng ở mức chữ (đúng G-5).

### 6.5 Vector (tuỳ chọn, mặc định tắt)

Tìm theo nghĩa ("chỗ nào tính hạn mức theo ngày") đôi khi hơn tìm chữ, nhưng cần mô hình embedding và gửi mã ra ngoài
hoặc chạy mô hình cục bộ. Thiết kế chừa cổng `Embedder` và bảng `code_vectors`; `code_search` gộp kết quả khi bật.
Không bật thì không có gì chạy. Lý do mặc định tắt: tìm chữ + đồ thị đã đủ cho phần lớn câu hỏi về code, và nhiều đơn
vị không cho gửi mã ra dịch vụ ngoài.

### 6.6 Trạng thái và độ tươi

```python
class IndexState(BaseModel):
    repo: RepoRef
    indexed_commit: str | None
    head_commit: str | None
    state: str            # none | indexing | ready | stale | error
    level: str            # text | symbols | graph
    files: int; symbols: int; edges: int
    took_s: float; error: str | None
```

Mọi câu trả lời của `code_*` kèm `commit` nó dựa trên. Nếu `indexed_commit != head_commit`, câu trả lời ghi rõ "chỉ mục
ở commit abc, nhánh chính đã tới def (3 commit)". `code_read` luôn đọc trực tiếp ở commit được yêu cầu, không qua chỉ
mục, nên bằng chứng ghim của Doc Core không bao giờ dựa trên dữ liệu cũ.

## 7. Đồ thị code (G-2)

### 7.1 Mô hình

| Loại nút | Ví dụ |
|---|---|
| `repo` | `acme/payments` |
| `service` | `payments-api` (đơn vị triển khai được) |
| `module` | module Maven/Gradle, package npm, package Python |
| `file` | `src/main/java/.../LimitService.java` |
| `symbol` | lớp, interface, phương thức, hàm |
| `endpoint` | `POST /v1/payments/{id}/refund` |
| `topic` | `payments.refund.requested` (Kafka), hàng đợi RabbitMQ |
| `table` | `payment`, `refund` |
| `external` | hệ thống ngoài không có mã ở đây: `vnpay`, `core-banking` |
| `config_key` | `clients.ledger.base-url` |

| Loại cạnh | Từ → tới | Nguồn |
|---|---|---|
| `contains` | repo → service → module → file → symbol | cấu trúc |
| `imports` | file → file / module | tree-sitter |
| `calls` | symbol → symbol | tree-sitter + phân giải |
| `extends`, `implements` | symbol → symbol | tree-sitter |
| `exposes` | symbol → endpoint | extractor (Spring, Express, FastAPI, OpenAPI) |
| `calls_http` | symbol → endpoint hoặc external | extractor (Feign, RestTemplate/WebClient, axios, httpx) |
| `publishes`, `subscribes` | symbol → topic | extractor (Kafka, RabbitMQ) |
| `reads`, `writes` | symbol → table | extractor (JPA, MyBatis XML + annotation, SQL thô) |
| `depends_on` | module → module / thư viện | file build |
| `configured_by` | symbol → config_key | extractor |

Mỗi cạnh có `evidence` (`path:line` ở commit được chỉ mục) và `confidence`:

| Mức | Nghĩa | Ví dụ |
|---|---|---|
| `exact` | Đọc thẳng từ cú pháp | `@PostMapping("/refund")`, `import` |
| `resolved` | Phân giải được qua import + tên | `limitService.check()` → `LimitService.check` |
| `heuristic` | Khớp theo tên hoặc chuỗi | URL ghép từ biến cấu hình; tên topic trong hằng số |

Truy vấn mặc định trả cả ba và ghi mức; giao diện vẽ cạnh `heuristic` bằng nét đứt. Không giấu phần không chắc, cũng
không trình bày nó như sự thật.

### 7.2 Phân giải lời gọi

Tree-sitter không có thông tin kiểu. Phân giải theo thứ tự: cùng file → cùng lớp → theo import → theo kiểu khai báo của
trường/tham số (đọc từ cú pháp) → theo tên duy nhất trong module. Không ra thì cạnh trỏ tới nút `symbol` chưa phân giải
mang tên gọi, mức `heuristic`.

**Nguồn chính xác (tuỳ chọn):** nếu CI của đơn vị sinh chỉ mục SCIP (từ trình biên dịch), khai
`precise: {scip_artifact: …}`; `graph.py` nạp nó và ghi đè cạnh `calls`/`implements` bằng mức `exact`. Không có thì dùng
tree-sitter. Office không tự build dự án để lấy thông tin kiểu.

### 7.3 Extractor (plugin trích quan hệ)

```python
class Extractor(Protocol):
    name: str
    def applies(self, repo: RepoFacts) -> bool: ...                         # có pom.xml + spring-boot? có package.json + express?
    def extract(self, file: ParsedFile, repo: RepoFacts) -> list[Fact]: ... # Fact = nút hoặc cạnh kèm evidence + confidence
```

Đăng ký qua entry point `actoroffice.extractors`. Có sẵn trong đợt đầu:

| Extractor | Trích |
|---|---|
| `spring` | `@RestController` + mapping → `exposes`; `@FeignClient`, `RestTemplate`, `WebClient` → `calls_http`; `@KafkaListener`, `KafkaTemplate`, `@RabbitListener` → topic; `@Value`, `@ConfigurationProperties` → `configured_by` |
| `mybatis` | Mapper XML (`<select>`, `<insert>`…) và annotation → `reads`/`writes` theo tên bảng; nối `namespace` + `id` với phương thức interface mapper |
| `jpa` | `@Entity`, `@Table`, repository → `reads`/`writes` |
| `openapi` | File OpenAPI/Swagger trong repo → `endpoint` mức `exact`, kèm schema tóm tắt |
| `http_client` | axios/fetch/httpx/requests với URL hằng hoặc từ cấu hình → `calls_http` |
| `express`, `fastapi` | route → `exposes` |
| `deploy` | Dockerfile, docker-compose, manifest k8s, Helm → nút `service`, biến môi trường trỏ service khác, cổng |
| `build_deps` | `pom.xml`, `build.gradle`, `package.json`, `pyproject.toml` → `module`, `depends_on` |

### 7.4 Truy vấn (cổng `CodeIntel`)

```python
class CodeIntel(Protocol):
    async def search(self, q: str, scope: Scope, kinds: list[str] | None = None, limit: int = 20) -> list[Hit]: ...
    async def outline(self, repo: RepoRef, path: str) -> Outline: ...                 # ký hiệu + chữ ký + dòng, không thân hàm
    async def symbol(self, name: str, scope: Scope) -> list[SymbolInfo]: ...          # định nghĩa + chữ ký + docstring
    async def references(self, symbol_id: str, direction: str, depth: int = 1) -> list[Edge]: ...   # callers | callees
    async def neighbors(self, node_id: str, edge_kinds: list[str], depth: int = 1) -> Subgraph: ...
    async def impact(self, targets: list[str], scope: Scope) -> Impact: ...           # file/symbol đổi → endpoint, topic, bảng, service bị chạm
    async def service_map(self, scope: Scope, focus: str | None = None) -> Subgraph: ...
    async def pack(self, question: str, scope: Scope, budget_tokens: int = 6000) -> ContextPack: ...
    async def state(self, scope: Scope) -> list[IndexState]: ...
```

### 7.5 Đọc code ít token: `pack` và cách AI dùng

Hiện AI hiểu code bằng `Grep` rồi `Read` cả file. Với chỉ mục, thứ tự đọc là:

1. `code_search` → vài đoạn trúng, mỗi đoạn kèm ký hiệu chứa nó.
2. `code_outline(path)` → khung file (chữ ký, không thân), thường bằng 5–10% kích thước file.
3. `code_symbol` / `code_refs` → đúng hàm cần, ai gọi nó, nó gọi ai.
4. `code_read(path, lines)` → chỉ đoạn cần trích làm bằng chứng.

`pack(question, budget)` làm sẵn bốn bước cho một câu hỏi: chọn ký hiệu trúng nhất, thêm hàng xóm một bước (ai gọi, gọi
ai, endpoint/bảng liên quan), xếp hạng, cắt theo ngân sách token, trả về kèm trích dẫn. Dùng ở đầu bước phân tích của
epic để AI có bức tranh trước khi đọc sâu.

Mọi đoạn trả về kèm trích dẫn đúng cú pháp `Nguồn:` của Doc Core (`repo:path:start-end`) và `commit`. AI chép thẳng vào
tài liệu, Doc Core ghim được ngay.

`code_impact` là chỗ nối với Doc Core: design khai `C-2` đụng `LimitService.check` → `impact` trả về endpoint, topic,
bảng và service bị chạm. Luật đối chiếu `X-DESIGN-CODE` dùng kết quả này để hỏi "design có nhắc tới service X cũng gọi
endpoint này không".

## 8. Bản đồ dịch vụ (G-3)

### 8.1 Service là gì và tìm ra thế nào

Service = một đơn vị triển khai được. Một repo có thể có nhiều service (monorepo), một service thuộc đúng một repo.

Thứ tự xác định:
1. **Khai báo** trong `.office/services.yaml` của repo hoặc trong hồ sơ project (đơn vị nói sao thì là vậy).
2. **Phát hiện** bởi extractor `deploy` và `build_deps`: mỗi Dockerfile, mỗi module có lớp `@SpringBootApplication`, mỗi
   `package.json` có script `start`, mỗi Deployment k8s.
3. Không có gì: cả repo là một service mang tên repo.

```yaml
# .office/services.yaml  (tuỳ chọn; để sửa điều máy đoán sai)
services:
  - name: payments-api
    path: services/payments
    owners: [team-pay]
aliases:                          # tên trong cấu hình/URL ↔ service
  ledger: ledger-service
  "http://core-gw.internal": core-banking
external:                         # hệ thống ngoài, không có mã
  - {name: vnpay, kind: payment-gateway}
edges:                            # cạnh máy không thấy được (gọi qua gateway, cấu hình ngoài repo)
  - {from: payments-api, to: notification-service, kind: calls_http, note: "qua API gateway"}
ignore:
  - {from: payments-api, to: legacy-batch}
```

### 8.2 Nối cạnh giữa các service

`codeintel/services.py` chạy sau khi mọi repo trong phạm vi đã có đồ thị:

| Loại | Cách khớp | Mức |
|---|---|---|
| HTTP | `calls_http` có đường dẫn khớp một `endpoint` (method + mẫu đường dẫn) **và** đích nhận ra được là service đó (tên Feign client, khoá cấu hình base-url, alias) | `resolved` |
| HTTP | Chỉ khớp đường dẫn, không biết đích | `heuristic` |
| Sự kiện | Cùng tên topic: bên `publishes` → bên `subscribes` | `exact` nếu tên là hằng chuỗi; `heuristic` nếu ghép từ cấu hình |
| Dữ liệu | Hai service cùng `reads`/`writes` một bảng | `exact`; đánh dấu "dùng chung bảng" |
| Thư viện | `depends_on` giữa module của hai repo | `exact` |
| Không khớp được | `calls_http` tới host không thuộc service nào | nút `external` |

Cạnh khai trong `.office/services.yaml` mang mức `declared` và thắng mọi suy đoán.

### 8.3 Trang "Bản đồ dịch vụ"

Vị trí: web, mục team, cạnh danh sách ticket. Ba mức phóng:

| Mức | Nút | Dùng để |
|---|---|---|
| Hệ thống | service, external, topic, bảng dùng chung | Thấy ai gọi ai, qua gì |
| Service | module, endpoint, topic, bảng của một service | Thấy bề mặt của một service |
| Module | file, ký hiệu chính | Lần tới code |

Thao tác:
- Lọc theo loại cạnh (HTTP / sự kiện / dữ liệu / thư viện) và theo mức tin cậy.
- Bấm một cạnh: danh sách bằng chứng (`file:dòng`), mở được đoạn code. Mọi cạnh trên bản đồ đều lần về dòng code.
- Bấm một nút: endpoint, topic, bảng, chủ sở hữu, ticket đang đụng tới nó.
- **Lớp phủ theo epic/task:** tô service và endpoint mà design của task khai là sẽ sửa (từ các mục `C-n` qua `impact`),
  và vùng ảnh hưởng một bước quanh đó. Lead duyệt design thấy ngay phạm vi.
- **So hai commit:** cạnh và nút thêm/bớt giữa nhánh chính và nhánh của ticket.
- Cạnh `heuristic` nét đứt; nút `external` khác màu; phần chỉ mục cũ có nhãn commit.
- Xuất Mermaid (để dán vào design, Confluence) và JSON.

Kỹ thuật: frontend hiện là HTML/JS tĩnh không có bước build (`web/static/`). Dùng Cytoscape.js chép vào `web/static/vendor/`
(không tải từ CDN, vì office có thể chạy trong mạng kín), bố cục `dagre` cho mức hệ thống. API:

```
GET /api/teams/{team}/map?level=system&focus=<node>&depth=1&edges=http,event&min_confidence=resolved
GET /api/teams/{team}/map/edge/{id}            → bằng chứng
GET /api/teams/{team}/map/overlay?ticket=<key> → nút/cạnh được tô
GET /api/teams/{team}/map/diff?base=<sha>&head=<sha>
GET /api/teams/{team}/map.mmd                  → Mermaid
```

Đồ thị lớn: máy chủ luôn trả một đồ thị con (theo `focus` + `depth`), không bao giờ gửi cả đồ thị ký hiệu. Mức hệ thống
gộp mọi lời gọi giữa hai service thành một cạnh có số đếm.

## 9. MCP (G-1)

Có ba vai trò của MCP, không nên lẫn:

| Vai trò | Thiết kế | Trạng thái |
|---|---|---|
| **(a) Office phục vụ tool cho AI của nó** | Bộ tool mục 9.1 qua MCP server `office` hiện có (`runtimes/office_mcp.py` cho Kiro, MCP trong tiến trình cho Claude). Đây là đường chính để AI truy cập Bitbucket, GitHub, Jira, Confluence | Mở rộng cái đang có |
| **(b) Gắn MCP server của bên ngoài** | `TeamSettings.mcp_servers` hiện có, mở rộng phân lớp office → team → project. Dùng khi đơn vị muốn AI gọi thẳng MCP chính thức của GitHub hoặc Atlassian, hoặc hệ thống office chưa có adapter. Adapter tổng quát `mcp` (Doc Core 7.6) biến một tool MCP thành nguồn tài liệu được theo dõi "đã đọc" | Mở rộng cái đang có |
| **(c) Office là MCP server cho công cụ ngoài** | Endpoint MCP qua HTTP cho IDE của dev (Claude Code, Cursor, Kiro) để hỏi chỉ mục, đồ thị, bản đồ dịch vụ, tài liệu của ticket. Chỉ đọc, xác thực bằng token theo người dùng | Mới, làm sau cùng |

Vì sao (a) là chính chứ không phải (b): office cần tự lấy nội dung để ghim bằng chứng, kiểm độ tươi, dựng chỉ mục, theo
dõi nguồn đã đọc. Những việc đó phải chạy không có mô hình và cho cùng kết quả mỗi lần, nên cần adapter API của chính
office. (b) vẫn có và không bị hạn chế; nó là đường bổ sung, không phải nền.

Cần xác minh lúc làm: MCP server chính thức của GitHub và của Atlassian hiện cung cấp những tool nào, có bao phủ
Bitbucket không, và cách xác thực. Thiết kế này không phụ thuộc câu trả lời, vì đường (a) dùng REST API.

### 9.1 Bộ tool

| Tool | Tham số chính | Trả về | Thay cho |
|---|---|---|---|
| `source_get` | `ref` | Nội dung markdown, phiên bản, liên kết, phần không đọc được | `jira_issue`, `wiki_page` |
| `source_search` | `query`, `kinds?` | Danh sách tham chiếu + trích đoạn | `wiki_search` |
| `code_search` | `query`, `repo?`, `kinds?` | Đoạn trúng + ký hiệu chứa + trích dẫn | `Grep` trên repo lớn |
| `code_outline` | `repo`, `path` | Khung file | `Read` cả file |
| `code_symbol` | `name` | Định nghĩa, chữ ký, vị trí | |
| `code_refs` | `symbol`, `direction`, `depth?` | Ai gọi / gọi ai, kèm mức tin cậy | |
| `code_read` | `repo`, `path`, `lines?`, `at?` | Đoạn code ở đúng commit + trích dẫn | |
| `code_impact` | `targets[]` | Endpoint, topic, bảng, service bị chạm | |
| `code_pack` | `question`, `budget?` | Gói ngữ cảnh theo ngân sách token | |
| `service_map` | `focus?`, `depth?` | Đồ thị con dạng chữ + Mermaid | |
| `repo_list` | `query?` | Repo trong phạm vi + trạng thái chỉ mục | |
| `pr_create`, `pr_reply`, `jira_comment` | như hiện nay | | Giữ, chuyển sang cổng mới |

Tool cũ (`jira_issue`, `wiki_page`, `wiki_search`) giữ làm bí danh một phiên bản rồi bỏ.

`ToolDef` thêm metadata để bỏ các tập tên tool gắn cứng (`engine/activity.py:22`, `engine/gate.py:30`,
`services/sources.py:30`):

```python
@dataclass(frozen=True)
class ToolDef:
    ...
    effect: str = "read"          # read | write | external
    activity: str = "office"      # read | search | source | edit | shell | office
    reads_source: Callable[[dict], DocRef | None] | None = None   # tool này đọc nguồn nào (để tính "đã đọc")
    needs: set[Capability] = frozenset()                           # thiếu kết nối thì tool không được đưa vào lượt chạy
```

`needs` thay cho việc luôn thêm `ATLASSIAN_TOOLS` ở `engine/executor.py:149`: team không có kết nối Jira thì AI không
thấy tool Jira, đỡ token và đỡ gọi nhầm.

## 10. Cấu hình

Một khối, phân lớp office → team → project (cùng quy tắc ghi đè với `doc_profile` của Doc Core). Mọi khoá đều có mặc
định; không khai gì thì office chạy như hiện nay với thư mục cục bộ.

```yaml
connections:
  - id: jira-main
    use: jira_cloud
    scope: office
    config: {site: "https://acme.atlassian.net", projects: [PAY, CORE], fields: {acceptance: customfield_10042}, poll_s: 120}
    secret_ref: atlassian
  - id: wiki-main
    use: confluence_cloud
    scope: office
    config: {site: "https://acme.atlassian.net", spaces: [PAY, ARCH], mirror: false, follow_links: {depth: 1, max: 20}}
    secret_ref: atlassian
  - id: bb-acme
    use: bitbucket_cloud
    scope: team:pay
    config: {workspace: acme, repos: ["payments", "ledger-*"]}
    secret_ref: bitbucket
    write: true
  - id: gh-acme
    use: github
    scope: team:platform
    config: {base_url: "https://api.github.com", org: acme, repos: ["*"], auth: app}   # auth: token | app
    secret_ref: github-app

code_intel:
  enabled: true
  source_order: [local, mirror, api]        # mục 5.3
  level: graph                              # text | symbols | graph
  languages: [java, kotlin, typescript, python]
  extractors: [spring, mybatis, jpa, openapi, http_client, deploy, build_deps]
  max_file_kb: 512
  ignore: ["**/generated/**", "**/*.min.js"]
  refresh: {on_push: true, poll_s: 300}
  precise: null                             # {scip_artifact: "ci://…"} khi có
  vectors: {enabled: false}

service_map:
  scope_repos: ["payments", "ledger-service", "notification-service"]   # những repo vẽ chung một bản đồ
  min_confidence: heuristic                 # mức thấp nhất được hiển thị

mcp:
  external: {}                              # như mcp_servers hiện nay; giờ khai được ở cả office và project
  serve: {enabled: false}                   # vai trò (c)
```

Kiểm khi lưu: adapter có tồn tại, `config` đúng `config_schema`, `secret_ref` có file, `scope` có thật, hai kết nối không
trùng `id`. Sai thì từ chối, cấu hình đang chạy giữ nguyên (cùng nguyên tắc với pack của Doc Core).

## 11. Dữ liệu

Chỉ mục nằm ở **file riêng** `<data_dir>/index.db`: nó lớn, dựng lại được từ nguồn, và không nên làm phình hay khoá
`office.db`. Xoá file thì chỉ mất thời gian dựng lại. Kết nối và cache nguồn nằm ở `office.db`.

```sql
-- office.db · migration 0019_connections.sql
CREATE TABLE connections (
  id TEXT PRIMARY KEY, use TEXT NOT NULL, scope TEXT NOT NULL,
  config_json TEXT NOT NULL DEFAULT '{}', secret_ref TEXT,
  enabled INTEGER NOT NULL DEFAULT 1, can_write INTEGER NOT NULL DEFAULT 0,
  health_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE source_cache (
  connection_id TEXT NOT NULL, ref TEXT NOT NULL, version TEXT NOT NULL, etag TEXT,
  title TEXT NOT NULL, body TEXT NOT NULL, meta_json TEXT NOT NULL DEFAULT '{}', fetched_at TEXT NOT NULL,
  PRIMARY KEY (connection_id, ref));

CREATE TABLE webhook_events (
  id INTEGER PRIMARY KEY, connection_id TEXT NOT NULL, kind TEXT NOT NULL,
  payload_json TEXT NOT NULL, received_at TEXT NOT NULL, handled_at TEXT);

-- index.db
CREATE TABLE repos (
  id INTEGER PRIMARY KEY, connection_id TEXT, slug TEXT NOT NULL, project_id TEXT,
  default_branch TEXT NOT NULL, indexed_commit TEXT, head_commit TEXT,
  state TEXT NOT NULL DEFAULT 'none', level TEXT NOT NULL DEFAULT 'text', stats_json TEXT, error TEXT, updated_at TEXT,
  UNIQUE (connection_id, slug));

CREATE TABLE files (
  id INTEGER PRIMARY KEY, repo_id INTEGER NOT NULL, path TEXT NOT NULL, blob_sha TEXT NOT NULL,
  lang TEXT, kind TEXT NOT NULL, size INTEGER NOT NULL,
  UNIQUE (repo_id, path));

CREATE VIRTUAL TABLE chunks USING fts5(text, path UNINDEXED, repo_id UNINDEXED, file_id UNINDEXED,
  start_line UNINDEXED, end_line UNINDEXED, symbol UNINDEXED, tokenize = 'trigram');

CREATE TABLE nodes (
  id INTEGER PRIMARY KEY, repo_id INTEGER, kind TEXT NOT NULL, name TEXT NOT NULL, qualified TEXT NOT NULL,
  file_id INTEGER, start_line INTEGER, end_line INTEGER, signature TEXT, meta_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX idx_nodes_kind_name ON nodes (kind, name);
CREATE INDEX idx_nodes_file ON nodes (file_id);
CREATE INDEX idx_nodes_qualified ON nodes (qualified);

CREATE TABLE edges (
  id INTEGER PRIMARY KEY, src INTEGER NOT NULL, dst INTEGER NOT NULL, kind TEXT NOT NULL,
  confidence TEXT NOT NULL, file_id INTEGER, line INTEGER, source TEXT NOT NULL,   -- source: tên extractor | "declared" | "scip"
  meta_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX idx_edges_src ON edges (src, kind);
CREATE INDEX idx_edges_dst ON edges (dst, kind);
CREATE INDEX idx_edges_file ON edges (file_id);            -- xoá nhanh cạnh của file đổi
```

Cập nhật tăng dần: file đổi → xoá `chunks`, `nodes`, `edges` theo `file_id` → phân tích lại → phân giải lại các cạnh
`calls` chưa phân giải trỏ theo tên vào ký hiệu vừa đổi. Cạnh liên service (`source = 'link'`) dựng lại toàn bộ sau mỗi
lượt, vì rẻ (chỉ ở mức endpoint/topic/bảng).

Bảng không có khoá ngoại vật lý, nối bằng id như các bảng hiện có.

## 12. Bảo mật và riêng tư

| Rủi ro | Biện pháp |
|---|---|
| Lộ token | Chỉ trong `secrets/`, đăng ký với `Redactor`, không vào prompt, log, `.git/config`, sự kiện |
| Token quá quyền | `required_scopes` tối thiểu; `test()` báo quyền thừa và thiếu; mặc định chỉ đọc |
| Chỉ dẫn độc trong tài liệu/mã lấy về | Nội dung ngoài là không tin cậy (taint): sau khi đọc, hành động ra ngoài phải hỏi người (luật hiện có) |
| SSRF | Adapter chỉ gọi host của kết nối; URL tự do không bao giờ đi kèm token |
| Webhook giả | Kiểm chữ ký theo từng hãng; sai thì bỏ và ghi audit |
| Đọc nhầm phạm vi | `projects` / `spaces` / `repos` là danh sách cho phép; `in_scope` kiểm trước khi gọi; ticket team khác vẫn "ngoài phạm vi" như SI-39 |
| Mã rời khỏi máy | Mirror và chỉ mục chỉ ở `<data_dir>`; vector mặc định tắt; vai trò MCP (c) mặc định tắt |
| Bí mật nằm trong mã được chỉ mục | Bỏ qua file khớp mẫu bí mật (`.env`, `*.pem`, `*credentials*`); đoạn trả về đi qua `Redactor` |
| Gỡ kết nối | Xoá `source_cache`, mirror, và phần chỉ mục của kết nối đó |

Mọi lần gọi ra ngoài ghi vào `AuditLog` hiện có: kết nối, hành động, đích, kết quả; không ghi nội dung.

## 13. Vận hành

- **Lập lịch:** đồng bộ và dựng chỉ mục là việc nền của `CodeIntelService`, một repo một lúc, ưu tiên thấp hơn lượt chạy
  của ticket. Có `office index status | rebuild <repo> | drop <repo>`.
- **Quan sát:** `IndexState` hiện ở Cài đặt › Kết nối và ở đầu trang Bản đồ. Sự kiện `index.started/finished/failed` qua
  `EventBus`.
- **Hỏng một phần:** một file phân tích lỗi thì ghi vào `stats_json.errors`, bỏ qua file đó, không làm hỏng cả lượt.
  Một extractor ném lỗi thì tắt extractor đó cho lượt này và báo.
- **Kích thước và thời gian, đo trên chính repo này** (2026-11-13, `scenarios/code_intel_live.py`, commit `9cbc503`):
  253 file được chỉ mục, 3 517 ký hiệu, 7 762 cạnh; lần đầu 7–8 s; lượt không có gì đổi 0,3 s; `index.db` 14,5 MB; mỗi
  truy vấn 0,3–0,7 s. Chưa đo trên repo Java cỡ vừa.

## 13b. Trạng thái hiện thực (2026-11-14)

| Phần | Trạng thái |
|---|---|
| `ports/`, `adapters/registry.py`, `ConnectionService`, migration `0018_connections.sql` | Xong, có test |
| Adapter `jira_cloud`, `confluence_cloud`, `bitbucket_cloud`, `github` | Xong với transport giả. **Chưa gọi hệ thống thật lần nào**. Các điểm V-1 … V-5 đã đối chiếu với tài liệu của hãng ngày 2026-11-14 và đã sửa theo (mục 16); vài điểm tài liệu không nói vẫn còn mở, ghi ở cuối mục 16 |
| `codeintel/`: nguồn (git, mirror, thư mục), chỉ mục chữ, ký hiệu (Python, Java, JS/TS), đồ thị, truy vấn, bản đồ dịch vụ | Xong, có test, đã chạy thật trên repo này |
| Extractor | Có `fastapi`, `http_client`, `spring` (gồm `@Table`), `mybatis`, `build_deps`, `openapi` (OpenAPI 3 / Swagger 2, YAML và JSON, một file), `express` (route Express, `router.route()`, tiền tố `app.use` trong cùng file; controller NestJS), `jpa` (repository Spring Data, `@Query` JPQL và native), `deploy` (manifest k8s, docker-compose, Helm `values.yaml`; template Helm có `{{ }}` không phải YAML thì bỏ qua). Có test trên mẫu nhỏ (`tests/test_extractors_more.py`), **chưa chạy trên dự án thật nào**. Còn thiếu: `jpa` không thấy `@Table(name=…)` của entity ở file khác nên tên bảng đoán theo snake_case (mức `heuristic`); router Express gắn tiền tố ở file khác thì đường dẫn thiếu tiền tố (mức `heuristic`); `deploy` chưa đọc Dockerfile |
| Tool `repo_list`, `code_search`, `code_outline`, `code_symbol`, `code_refs`, `code_read`, `code_impact`, `code_pack`, `service_map` | Xong, mọi lượt chạy ticket đều có; đã thử với Kiro CLI thật |
| Tool `source_get` / `source_search` (`services/sourcehub.py`, `tools/source_tools.py`) | Xong, có test với adapter giả: đọc qua kết nối gần nhất, lưu bản sao theo phiên bản, ghi nhận "đã đọc", làm phiên chạy thành "không tin cậy". `ToolDef` metadata (`effect`, `activity`, `reads_source`, `needs`, thêm `untrusted`) xong, có test (`tests/test_tool_meta.py`): các tập tên tool office gắn cứng ở `engine/activity.py`, `engine/gate.py`, `services/sources.py`, `security/policy.py` đã bỏ, tra qua `tools/registry.py: tool_def()`. `needs`: tool Jira / Confluence / PR chỉ vào lượt chạy khi phạm vi ticket có kết nối đủ năng lực, **hoặc** còn cấu hình cũ (`secrets/jira.json`, `secrets/bitbucket.json`); team không có cả hai thì không thấy các tool đó. `source_get` / `source_search` cố ý không khai `needs`: luôn có, chưa kết nối thì trả lời thiếu gì (mục 3.4). `jira_issue`, `wiki_page`, `wiki_search` là bí danh: có kết nối thì đi qua `sourcehub` / adapter (ghi "đã đọc", làm phiên "không tin cậy"), chỉ có cấu hình cũ thì chạy như trước. `jira_search`, `jira_comment`, `pr_create`, `pr_comments`, `pr_reply` và bộ theo dõi PR dùng kết nối `Tracker` / `CodeHost` (việc ghi cần `write`), không có thì dùng đường cũ; URL PR của GitHub được nhận qua `parse_pr_url` của adapter. Đường cũ (`services/jira.py`, `services/atlassian.py`) **chưa gỡ**, là đường dự phòng; chuyển sang kết nối bằng lệnh tay `actoroffice connections import-legacy` (`ConnectionService.import_legacy`, không tự chạy). Còn dùng riêng cấu hình cũ: đồng bộ Jira → ticket (`JiraMirror`), xuất bản Confluence (`knowledge.publish`), nút merge và check `pr_linked` (`office.atlassian.code_host()`); prompt giai đoạn vẫn nhắc tên tool Jira / wiki kể cả khi lượt chạy không có chúng |
| Web: Kết nối, Bản đồ dịch vụ; API `/api/connections`, `/api/index`, `/api/map`, `/api/map/overlay`, `/api/map/diff` | Xong, có test API (`tests/test_intelweb.py`, `tests/test_maplevels.py`), đã kiểm trên trình duyệt. Đủ ba mức: hệ thống → service (`?service=`: module, endpoint, topic, bảng, service nó nói chuyện) → module (`&module=`: file và ký hiệu chính; module quá lớn thì vẽ theo file và đánh dấu rút gọn). Module = thư mục, tách dần thư mục chiếm phần lớn code (`codeintel/services.py::Modules`). So nhánh/commit với nhánh chính: `/api/map/diff?head=` hoặc `?ticket=` (nhánh của ticket); dựng trên một bản sao tạm của chỉ mục, chỉ đọc lại file khác nhau. Bản đồ vẽ bằng SVG tự viết (bố cục vòng tròn), **không dùng Cytoscape** như thiết kế: thêm thư viện ngoài cần tải file về. Chưa có: so sánh ở mức service/module, lọc theo mức tin cậy trên giao diện |
| Webhook (`POST /hooks/<kết nối>`, `services/webhooks.py`, migration `0023_webhook_events.sql`) | Xong, có test (`tests/test_webhooks.py`): adapter kiểm chữ ký, sai thì từ chối và ghi audit; push lên nhánh chính → lập chỉ mục lại repo tương ứng (project có `repo_url` khớp, hoặc repo đã chỉ mục từ kết nối đó); sự kiện PR → `PullRequestWatcher` kiểm ngay. `/hooks/` là đường duy nhất nhận host ngoài loopback. Chưa nhận delivery thật nào |
| Chỉ mục nền (`CodeIntelService.run_forever`, cấu hình `index_sweep_s`, mặc định 300 giây) | Xong, có test. Nhường lượt chạy ticket: hoãn tối đa ba lượt khi đang có lượt chạy |
| Nguồn `api` (7b) (`codeintel/sync.py::ApiSource`) | Xong, có test với code host giả: kết nối nằm trong `code_api_connections` (hoặc host không cho clone) thì đọc cây và file qua API, chỉ nhánh chính, mỗi lượt chỉ hỏi lại file đổi. Chưa thử với GitHub / Bitbucket thật |
| Vector, SCIP | Chưa làm (thiết kế để tuỳ chọn, mặc định tắt) |
| Adapter tổng quát `http`, `mcp`, `command` (`adapters/generic/`, năng lực `docs`) | Xong, có test (`tests/test_adapters_generic.py`): nối một nguồn chỉ bằng cấu hình rồi đọc/tìm qua `sourcehub`; `mcp` và `command` chạy tiến trình con thật. Thay cho phần "adapter tổng quát" ở dòng trên. Khác Doc Core 7.6: `http` chỉ gọi host của `base_url` (đường dẫn, không phải URL tự do); `mcp` khai server ngay trong kết nối (cùng dạng một mục `mcp_servers`), chưa tham chiếu theo tên; `command` là nguồn tài liệu, chưa phải nguồn code. **Chưa nối hệ thống thật nào**; MCP qua SSE chưa thử |
| MCP vai trò (c): office là MCP server chỉ đọc cho IDE (`web/mcp_api.py`, `services/mcp_tokens.py`, migration `0024_mcp_tokens.sql`) | Xong, có test (`tests/test_mcp_server.py`). Thay cho phần "MCP vai trò (c)" ở dòng "Chưa làm" phía trên. Mặc định tắt (`mcp_server: true` để bật). `POST /mcp` nói JSON-RPC 2.0 trả JSON thường (tự viết, không dùng thư viện `mcp`; không stream, không phiên), xác thực bằng token cá nhân `Authorization: Bearer` (chỉ lưu SHA-256, hiện một lần; quản lý qua `/api/mcp/tokens`, `/api/mcp/info`, `actoroffice mcp-token`). 13 tool chỉ đọc: 9 tool code + `source_get`, `source_search` + `ticket_docs`, `ticket_doc`; phạm vi theo team của người giữ token hoặc theo `ticket` sau khi kiểm thành viên; `source_get` ở đây không ghi nhận "đã đọc"; mọi lần gọi ghi audit `mcp:call`. Còn giữ luật Host loopback của web nên IDE ở máy khác phải đi qua tunnel. **Chưa thử với máy khách MCP thật** (Claude Code, Cursor, Kiro); chưa có màn hình quản lý token trên web |

Khác thiết kế, có chủ ý:
- Ngôn ngữ khai bằng một mục trong `LANGS` (`codeintel/parse.py`), không phải file `.scm`: cần đi cây để có tên đầy đủ và
  quan hệ cha con, truy vấn `.scm` không cho được.
- Lời gọi tới thứ không định nghĩa trong repo (builtin, thư viện) **không tạo cạnh**, thay vì tạo nút chưa phân giải: nếu
  giữ thì đồ thị đầy nút `len`, `append`.
- Cạnh liên service tính lúc hỏi từ các cạnh sự kiện trong chỉ mục, không lưu sẵn: luôn theo đúng phạm vi team đang hỏi.
- Gói grammar: dùng từng gói `tree-sitter-<ngôn ngữ>` (có sẵn bản dựng cho Windows, chạy không cần mạng) thay cho gói gộp.
  Mục V-6 đã xong.

## 14. Thứ tự làm, mỗi bước có cách kiểm

| # | Việc | Kiểm bằng |
|---|---|---|
| 1 | `ports/`, `adapters/registry.py` (entry point, `config_schema`), `ConnectionService`, migration `0019`, bọc hạn mức/cache/lỗi | Test: adapter giả đăng ký và dựng từ cấu hình; cấu hình sai bị từ chối; `429` được thử lại; lỗi về đúng `kind`. Test import: `ports/` không import office |
| 2 | Chuyển `JiraClient`, `Confluence`, `BitbucketCloud` hiện có thành adapter; `ToolDef` thêm metadata; bỏ tập tên tool gắn cứng | Toàn bộ test Atlassian và SI-39 hiện có vẫn qua; team không có Jira thì lượt chạy không có tool Jira |
| 3 | `jira_cloud` đầy đủ (4.3), `confluence_cloud` đầy đủ (4.4), `storage_html.py`, đi theo liên kết (4.5) | Test với transport giả + bộ mẫu trang thật đã ẩn danh: bảng, macro, include, draw.io → `notes`; link Jira → Confluence được thêm làm nguồn |
| 4 | Web Cài đặt › Kết nối; trạng thái "chưa cấu hình" ở nguồn, tool, check (3.4) | Chụp màn hình; test: epic nhắc khoá Jira khi chưa kết nối → báo đúng lý do |
| 5 | `codeintel/sync.py`, `index.py` (manifest + FTS), `code_search`, `code_read`, `IndexState`; nguồn `local` | Test trên repo git tạm: push đổi 2 file → chỉ 2 file được chỉ mục lại; đo thời gian và kích thước trên repo này, ghi vào mục 13 |
| 6 | `parse.py` + truy vấn Java, TypeScript, Python; `graph.py`; `code_outline`, `code_symbol`, `code_refs` | Test bảng theo ngôn ngữ: file mẫu → ký hiệu và cạnh `calls` đúng; lời gọi không phân giải được mang mức `heuristic` |
| 7 | `github` adapter; `bitbucket_cloud` thêm phần đọc (head, compare, clone); nguồn `mirror`; webhook | Test transport giả cho từng API; test mirror với repo git cục bộ đóng vai remote; chữ ký webhook sai bị từ chối |
| 7b | (Làm sau, khi có đơn vị cấm clone) nguồn `api`: `tree`, `file` qua REST | Test transport giả: chỉ mục dựng từ API khớp chỉ mục dựng từ mirror trên cùng repo mẫu |
| 8 | Extractor: `build_deps`, `deploy`, `spring`, `mybatis`, `jpa`, `openapi`, `http_client` | Mỗi extractor một dự án mẫu nhỏ trong `tests/fixtures/`: endpoint, topic, bảng trích đúng, kèm dòng bằng chứng |
| 9 | `services.py` (nối liên service, `.office/services.yaml`), `code_impact`, `service_map`, `code_pack` | Test với 3 dự án mẫu gọi nhau qua HTTP + Kafka + bảng chung: cạnh đúng loại, đúng mức; khai báo thắng suy đoán |
| 10 | Trang Bản đồ dịch vụ + API + xuất Mermaid + lớp phủ theo ticket | Chụp màn hình với dữ liệu seed ở cả ba mức; bấm cạnh ra đúng dòng code |
| 11 | Nối Doc Core: `Nguồn:` dùng `code_read`; `X-DESIGN-CODE` dùng `impact`; thẻ duyệt design có bản đồ thu nhỏ | Test: design khai sửa một hàm → thẻ hiện service bị chạm; code đổi → bằng chứng thành `cu` |
| 12 | MCP vai trò (c); phân lớp `mcp.external`; adapter tổng quát `http`, `mcp`, `command` | Test: máy khách MCP gọi `code_search` bằng token người dùng; token sai bị từ chối |

Bước 1–4 cho G-4 và G-5. Bước 5–6 cho G-2 với thư mục cục bộ, chưa cần kết nối nào. Bước 7 cho G-1. Bước 8–10 cho G-3.
Sau bước 6 AI đã đọc code tốt hơn hiện nay; đó là điểm dừng có giá trị đầu tiên.

So với Doc Core: bước 1–2 ở đây **thay** cho bước 4 và 6 của Doc Core (cùng một registry và cùng việc chuyển
`sources_read`); không làm hai lần.

## 15. Quyết định

- **D-1. Adapter API là nền, MCP là mặt tiếp xúc với AI.** Phương án khác: chỉ gắn MCP server của hãng và để AI tự gọi.
  Bỏ vì office không tự ghim bằng chứng, dựng chỉ mục hay kiểm độ tươi được nếu mọi truy cập đều phải qua mô hình. Hệ
  quả: phải tự viết và bảo trì 4 adapter.
- **D-2. Tree-sitter mặc định, SCIP tuỳ chọn.** Phương án khác: language server / chỉ mục từ trình biên dịch cho mọi
  repo. Bỏ làm mặc định vì office phải build được từng dự án (JDK, Maven, mạng nội bộ). Hệ quả: cạnh `calls` có phần
  suy đoán, nên mỗi cạnh mang mức tin cậy và có đường nạp SCIP khi đơn vị có.
- **D-3. SQLite + FTS5 trigram, file riêng.** Phương án khác: máy tìm kiếm ngoài (Zoekt, OpenSearch) hoặc cơ sở dữ liệu
  đồ thị. Bỏ vì thêm hạ tầng phải vận hành; quy mô vài chục repo vẫn trong tầm SQLite. Cổng `CodeIntel` cho phép đổi
  ruột sau nếu quy mô vượt.
- **D-4. Vector mặc định tắt.** Lý do ở 6.5.
- **D-5. Mức tin cậy trên mọi cạnh, không giấu cạnh suy đoán.** Bản đồ thiếu cạnh thì nguy hiểm hơn bản đồ có cạnh nét đứt.
- **D-6. Khai báo của người thắng suy đoán của máy** (`.office/services.yaml`), và file đó nằm trong repo để được review
  như code.
- **D-7. Tool thiếu kết nối thì không đưa vào lượt chạy** (`ToolDef.needs`), thay cho việc luôn nạp rồi báo lỗi khi gọi.
- **D-8. Mirror tách khỏi worktree làm việc.** Chỉ mục luôn phản ánh nhánh chính, không bị thay đổi dở dang của ticket làm bẩn.
  So sánh nhánh ticket với nhánh chính làm bằng `compare`, không bằng chỉ mục thứ hai.
- **D-9. Chưa làm:** Bitbucket Data Center, GitLab, Azure DevOps, đồng bộ ticket hai chiều, phân tích luồng dữ liệu
  trong hàm. Cổng đã có chỗ.

## 16. Đối chiếu với tài liệu của hãng (2026-11-14)

Các điểm dưới đây đã được đối chiếu ngày 2026-11-14 với tài liệu công khai và bản mô tả OpenAPI mà từng hãng công bố.
**Vẫn chưa gọi hệ thống thật lần nào** (không có tài khoản): "đã đối chiếu" nghĩa là khớp với tài liệu, không phải đã chạy thử.

| # | Điều | Kết quả | Tài liệu nói gì; code đổi gì | Nguồn |
|---|---|---|---|---|
| V-1 | Tên scope chính xác của Jira, Confluence, Bitbucket, GitHub | Đã đối chiếu, **có sửa** (Jira, Confluence) | **Jira** (scope classic, loại Atlassian khuyên dùng khi có): `read:jira-work` cho issue, tìm kiếm, bình luận, remote link; `read:jira-user` cho `/myself` mà `test()` gọi (**thêm**); `write:jira-work` để bình luận. **Confluence**: REST v2 chỉ nhận scope granular: `read:page:confluence`, `read:space:confluence`, `read:attachment:confluence`, `read:hierarchical-content:confluence` (trang con), `write:page:confluence` (xuất bản); hai lời gọi v1 còn giữ dùng scope classic `search:confluence` và `read:confluence-user` (**sửa**: bỏ `read:confluence-content.all`, `write:confluence-content`). **Bitbucket** (API token): `read:repository:bitbucket`, `read:pullrequest:bitbucket`, `write:pullrequest:bitbucket`: đúng; bình luận PR chỉ cần read, tạo PR cần write. **GitHub** (token fine-grained, quyền mức repo): Contents read, Metadata read, Pull requests read/write: đúng. | developer.atlassian.com/cloud/jira/platform/scopes-for-oauth-2-3LO-and-forge-apps · developer.atlassian.com/cloud/confluence/scopes-for-oauth-2-3LO-and-forge-apps · scope ghi ở từng lời gọi trong OpenAPI: developer.atlassian.com/cloud/jira/platform/swagger-v3.v3.json, dac-static.atlassian.com/cloud/confluence/openapi-v2.v3.json, dac-static.atlassian.com/cloud/confluence/swagger.v3.json, dac-static.atlassian.com/cloud/bitbucket/swagger.v3.json · support.atlassian.com/bitbucket-cloud/docs/api-token-permissions · docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens |
| V-2 | Base URL khi dùng token có phạm vi / OAuth của Atlassian | Đã đối chiếu, **có sửa**; còn hai điểm mở | API token **không scope**: gọi thẳng site (`https://<site>/rest/api/3/…`, `https://<site>/wiki/…`) với Basic `email:token`: như code cũ. API token **có scope** (loại Atlassian khuyên tạo): chỉ gọi được qua `https://api.atlassian.com/ex/jira/{cloudId}/…` và `https://api.atlassian.com/ex/confluence/{cloudId}/wiki/…`, vẫn Basic `email:token`; `cloudId` lấy ở `https://<site>/_edge/tenant_info`. OAuth 2.0 (3LO): cùng địa chỉ gateway, header Bearer, `cloudId` lấy từ `accessible-resources`. **Sửa**: thêm cấu hình tuỳ chọn `cloud_id` cho `jira_cloud` và `confluence_cloud`; có thì `atlassian/auth.py` dựng base URL qua gateway; khi bị 401 mà chưa khai `cloud_id`, `test()` nhắc khả năng này. OAuth vẫn chưa làm. **Còn mở**: (a) ví dụ của hãng cho token có scope chỉ nêu đường v1 (`/wiki/rest/api/…`); đường v2 `/wiki/api/v2/…` qua gateway là suy ra từ mẫu `<api>`, chưa trang nào nêu thẳng, kể cả dạng của `_links.next` khi đi qua gateway; (b) link rút gọn `/wiki/x/<mã>` không phải API nên không phân giải được qua gateway. | support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account · support.atlassian.com/confluence/kb/scoped-api-tokens-in-confluence-cloud · support.atlassian.com/jira/kb/retrieve-my-atlassian-sites-cloud-id · developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps · developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis |
| V-3 | Phiên bản API Confluence nên dùng và cách lấy storage format | Đã đối chiếu, **có sửa**; còn một điểm mở | Dùng **v2** cho trang: `GET /wiki/api/v2/pages/{id}?body-format=storage` trả `body.storage.value`, `spaceId`, `version.number`, `_links.webui`; tìm theo tiêu đề bằng `GET /pages?space-id=&title=`; tạo/sửa bằng `POST /pages`, `PUT /pages/{id}`; tệp đính kèm, space: đúng như code. `content/{id}` của v1 không còn trong tài liệu v1. Chỉ còn ở **v1**: tìm bằng CQL `GET /wiki/rest/api/content/search` và `GET /wiki/rest/api/user/current`: đúng như code. **Sửa**: `GET /pages/{id}/children` đã bị đánh dấu deprecated; đổi sang `GET /pages/{id}/direct-children` (trả mọi loại con kèm `type`, code chỉ giữ `type = page`). **Còn mở**: link rút gọn `/wiki/x/<mã>` không có trong tài liệu API; code vẫn dựa vào chuyển hướng của site, chưa kiểm được. | developer.atlassian.com/cloud/confluence/rest/v2/intro · dac-static.atlassian.com/cloud/confluence/openapi-v2.v3.json · dac-static.atlassian.com/cloud/confluence/swagger.v3.json |
| V-4 | Cách ký webhook của Bitbucket Cloud và GitHub | Đã đối chiếu, chữ ký **đúng**; **có sửa** phần đọc sự kiện | **Bitbucket**: header `X-Hub-Signature: sha256=<hex>`, HMAC-SHA256 trên thân nguyên văn, chỉ gửi khi webhook có secret; tên sự kiện ở `X-Event-Key`; `repo:push` (`push.changes[].new.type/name/target.hash`, `new` là null khi xoá nhánh), `pullrequest:created/updated/fulfilled/rejected/comment_created` (`pullrequest.id`, `source.branch.name`, `source.commit.hash`, `repository.full_name`): đúng. **GitHub**: `X-Hub-Signature-256: sha256=<hex>`, tên sự kiện ở `X-GitHub-Event`; `push` (`ref`, `after`, `deleted`), `pull_request` (`action`, `pull_request.number/merged/head`), `pull_request_review`, `pull_request_review_comment`: đúng. Test có thêm ví dụ mẫu của chính hai hãng. **Sửa**: Bitbucket: một lần push đổi nhiều ref thì lấy nhánh đầu tiên (trước đây gặp tag đứng đầu là bỏ cả sự kiện). GitHub: bình luận chung trên PR đến dưới dạng `issue_comment` có `issue.pull_request`, nay thành `pr_comment`. Webhook GitHub phải đặt content type `application/json`. | support.atlassian.com/bitbucket-cloud/docs/manage-webhooks · support.atlassian.com/bitbucket-cloud/docs/event-payloads · docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries · docs.github.com/en/webhooks/webhook-events-and-payloads |
| V-5 | MCP chính thức của GitHub/Atlassian có những tool gì, có Bitbucket không | Đã đối chiếu (chỉ ghi nhận, không có code) | **GitHub MCP Server**: bản remote `https://api.githubcopilot.com/mcp/` (OAuth hoặc PAT qua header Bearer), bản chạy tại chỗ `ghcr.io/github/github-mcp-server` có cờ `--read-only`. Toolset mặc định: `context`, `repos`, `issues`, `pull_requests`, `users`. `repos` có `get_file_contents`, `search_code`, `list_commits`, `get_commit`, `list_branches`, `create_branch`, `push_files`…; `pull_requests` có `pull_request_read`, `list_pull_requests`, `create_pull_request`, `add_reply_to_pull_request_comment`, `pull_request_review_write`, `merge_pull_request`…; `git` có `get_repository_tree`. **Atlassian MCP** (Rovo): `https://mcp.atlassian.com/v2/mcp`, OAuth 2.1 hoặc API token. Jira (`getJiraIssue`, `searchJiraIssuesUsingJql`, `listJiraIssueComments`, `addOrEditJiraIssueComment`…), Confluence (`getConfluenceContent`, `searchConfluence`, `createConfluenceContent`…), JSM và các sản phẩm khác. **Có Bitbucket Cloud**: nhóm `read_bitbucket` (`getBitbucketRepoFileContent`, `getBitbucketRepoPullRequest`, `listBitbucketRepoPullRequestComments`…) và `write_bitbucket` (`createBitbucketRepoPullRequest`, `addBitbucketRepoPullRequestComment`…), chỉ dùng được khi workspace Bitbucket đã gắn với một organization. | github.com/github/github-mcp-server · support.atlassian.com/atlassian-rovo-mcp-server/docs/supported-tools · support.atlassian.com/atlassian-rovo-mcp-server/docs/getting-started-with-the-atlassian-remote-mcp-server |
| V-6 | Gói grammar tree-sitter nào còn được bảo trì và cài được trên Windows | Đã xong từ trước (xem 13b), không thuộc đợt đối chiếu này | — | — |

Sửa thêm trong đợt đối chiếu, ngoài năm điểm trên:

- **Hạn mức** (`adapters/_http.py`). GitHub trả 403 hoặc 429 kèm `x-ratelimit-remaining: 0` và `x-ratelimit-reset` (giây epoch UTC), dặn không gọi
  lại trước mốc đó; Atlassian trả 429 kèm `Retry-After` có thể tới hàng nghìn giây. Nay: đọc cả `x-ratelimit-reset`; thời gian chờ được báo mà
  dài hơn một lần ngủ (60 giây) thì **không gọi lại**, trả lỗi `rate_limited` kèm `retry_after`. Trước đây ngủ 60 giây rồi gọi lại tới ba lần.
  Nguồn: docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api · developer.atlassian.com/cloud/jira/platform/rate-limiting.
- **Jira**. Tìm kiếm `POST /rest/api/3/search/jql` (phân trang bằng `nextPageToken`, phải nêu `fields`, JQL phải có điều kiện giới hạn): đúng; nay
  đi tiếp theo `nextPageToken` cả khi phản hồi thiếu `isLast`. `GET …/remotelink` trả 403 khi site tắt liên kết issue: nay coi là không có
  remote link, vẫn đọc được issue.
- **GitHub**. `GET /orgs/{org}/repos` chỉ dành cho tổ chức; với tài khoản cá nhân (404) nay liệt kê qua `GET /user/repos?affiliation=owner`. Trả
  lời một bình luận review phải nhằm vào bình luận đầu của luồng ("replies to replies are not supported"): `pr_reply` nay tra bình luận gốc
  trước; bình luận chung thì trả lời bằng bình luận chung. `X-GitHub-Api-Version: 2022-11-28` còn được hỗ trợ tới 2028-03-10 (bản mới hơn là
  `2026-03-10`, không thay đổi nào chạm tới trường code đang đọc): giữ nguyên. `compare` chỉ nhận về tối đa 300 file cho một lần so sánh.
- **Bitbucket**. `pagelen` nhỏ nhất theo tài liệu là 10: lời gọi kiểm tra kết nối đổi từ 1 sang 10. Thứ tự `<nguồn>..<đích>` của `diffstat`, tên
  người dùng git `x-bitbucket-api-token-auth`, Basic `email:token` cho API: đúng.

Còn mở (tài liệu không nói, chỉ biết khi gọi hệ thống thật):

- Bitbucket có gửi header `X-OAuth-Scopes` cho API token hay không (thiếu thì `test()` báo "không rõ quyền", không sai).
- Bitbucket trả gì khi vượt hạn mức ngoài mã 429 (trang hạn mức không nêu `Retry-After`); code lùi dần như cũ.
- GitHub vượt hạn mức phụ mà không kèm header nào: tài liệu dặn chờ ít nhất một phút, code chỉ lùi dần vài giây rồi báo lỗi.
- File do LFS quản lý trên Bitbucket được trả bằng chuyển hướng sang host khác: adapter từ chối, không tải.

## 17. Cần bạn chốt

1. ~~Ngôn ngữ và framework ưu tiên~~ — **đã chốt 2026-11-13:** Java + Spring + MyBatis trước, rồi TypeScript, rồi Python.
2. ~~Office có được clone repo về máy chạy nó không?~~ — **đã chốt 2026-11-13: được.** Mirror là đường chính cho repo
   trên Bitbucket/GitHub. Đường `api` (5.3) vẫn giữ trong cổng cho đơn vị khác, nhưng làm sau, không thuộc bước 7.
3. ~~Bản đồ dịch vụ vẽ theo phạm vi nào~~ — **đã chốt 2026-11-13: mỗi team một bản đồ.** Service của team khác mà team
   này gọi tới hiện là nút `external` (chỉ tên, loại cạnh và bằng chứng phía team này); không hiện mã, endpoint nội bộ
   hay cạnh bên trong của team kia.

Cả ba điểm đã chốt. Việc còn lại trước khi code là các điều cần xác minh ở mục 16.
