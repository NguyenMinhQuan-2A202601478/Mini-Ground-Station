# mini-ground-station

[![CI](https://github.com/NguyenMinhQuan-2A202601478/Mini-Ground-Station/actions/workflows/ci.yml/badge.svg)](https://github.com/NguyenMinhQuan-2A202601478/Mini-Ground-Station/actions/workflows/ci.yml)

Trạm mặt đất cho vệ tinh LEO. Vệ tinh mô phỏng bay quỹ đạo thật tính bằng SGP4
từ TLE, ghi telemetry suốt vòng quỹ đạo rồi dồn xuống trong những phút hiếm hoi
bay qua trạm. FastAPI nhận và lưu vào PostgreSQL, một worker riêng dò bất
thường và sinh cảnh báo, dashboard vẽ biểu đồ.

Phần cứng và RF là mô phỏng. Cơ học quỹ đạo thì không, và mọi thứ từ API nhận
dữ liệu trở xuống đều chạy thật.

### Đọc gì trong 5 phút đầu

Nếu bạn chỉ có vài phút, bốn file này nói được nhiều nhất về cách dự án được
nghĩ ra, chứ không chỉ nó làm gì:

| File | Vì sao đáng đọc |
|---|---|
| [`docs/product/schema.md`](docs/product/schema.md) | Schema được thiết kế trước khi viết dòng code nào. Giải thích vì sao chỉ có ba bảng: con trỏ của worker nằm ngay trên cột `telemetry.screened_at`, nên không cần bảng thứ tư để lưu trạng thái. |
| [`docs/decisions/0004`](docs/decisions/0004-real-orbit-propagation.md) | Mô hình quỹ đạo đầu tiên phải *gian lận* — dịch ground track lên trên trạm để vòng nào cũng có pass. Cú gian lận đó xoá mất chính bài toán: một trạm chỉ thấy vệ tinh khoảng 1% thời gian trong ngày. |
| [`docs/decisions/0006`](docs/decisions/0006-retention-not-partitioning.md) | Tại sao **không** partition bảng telemetry. Không phải ý kiến cá nhân — PostgreSQL từ chối, và thông báo lỗi của nó được trích nguyên văn. Partition sẽ đổi được xoá rẻ hơn bằng cách vứt đi tính nạp-đúng-một-lần. |
| [`docs/decisions/0001`](docs/decisions/0001-alerts-are-episodes.md) | 1.172 frame sinh ra **1.324 cảnh báo**, tức là vô dụng. Sửa thành cảnh báo theo đợt còn 60. Có số đo trước và sau. |

Job `Container stack` trong CI dựng nguyên hệ trong container, bay một ngày quỹ
đạo thật rồi kiểm tra kết quả. Chính những phép kiểm tra này đã phát hiện năm
lỗi mà unit test bỏ sót: crash khi screen lại, cảnh báo trùng ở ranh giới
batch, hai worker cùng cắt một luồng dữ liệu, `--once` bỏ sót 1.613 frame, và
cửa sổ huấn luyện neo nhầm vào đồng hồ hệ thống khiến việc replay dữ liệu cũ
im lặng không chấm gì cả. Chi tiết nằm trong lịch sử commit.

### Dự án này được viết thế nào

Viết trong một phiên làm việc với [Claude Code](https://claude.com/claude-code).
Bộ công cụ agent dùng trong phiên đó được giữ lại trong repo chứ không dọn đi:
`AGENTS.md` và `.agents/skills/` là giao thức làm việc của
[repository-harness](https://github.com/hoangnb24/repository-harness),
`.claude/skills/graphify/` là skill dựng đồ thị tri thức của
[graphify](https://github.com/Graphify-Labs/graphify) để tra cứu code thay cho
grep. Chi tiết trong [`docs/agents.md`](docs/agents.md).

Thứ đáng đọc không phải là công cụ, mà là **những gì được ghi lại**: sáu bản
quyết định trong `docs/decisions/` nói rõ phương án nào bị loại và vì sao, kèm
số đo trước và sau. Trong đó có một quyết định mà phương án ban đầu bị chính
PostgreSQL bác bỏ, và thông báo lỗi của nó được trích nguyên văn thay cho lời
giải thích.

*Phần còn lại của README bằng tiếng Anh.*

---

A miniature satellite ground station. A simulated spacecraft flies a **real
orbit** — SGP4 against a published TLE — and downlinks telemetry during the
contact windows that geometry actually gives it; a FastAPI service ingests
those frames into PostgreSQL; a background worker screens them and raises
alerts.

```
simulated satellite  ──HTTP──>  FastAPI ingestion  ──>  PostgreSQL
   (orbit + health model)          (passes, telemetry)      │
                                                            ├──> anomaly worker ──> alerts
                                                            └──> query API ──> dashboard
```

The hardware and RF layers are simulated. The orbital mechanics are not, and
everything from the ingestion API downwards is the real thing.

## Layout

| Path | What it is |
|---|---|
| `docs/product/schema.md` | **Read this first** — the authoritative data model |
| `src/mgs/models.py` | SQLAlchemy tables: `passes`, `telemetry`, `alerts` |
| `src/mgs/ingest.py` | Idempotent ingestion service (shared by API and tests) |
| `src/mgs/api/` | FastAPI app and routes |
| `src/mgs/api/static/` | The dashboard — one HTML page, no build step |
| `src/mgs/worker/` | Threshold rules, statistical detector, screening loop |
| `src/mgs/simulator/` | SGP4 propagation, TLE handling, spacecraft health model |
| `migrations/` | Alembic migrations |
| `Dockerfile`, `docker-compose.yml` | One image, three processes, one command |
| `.github/workflows/ci.yml` | Lint, tests, and a container smoke run |

## Quick start — containers

Nothing to install but Docker:

```bash
make up          # database, migrations, API, worker
```

Then open **<http://localhost:8000/>**. Migrations run as their own container
that must exit cleanly before the API or the worker start, so nothing ever
races a half-applied schema.

Feed it some telemetry:

```bash
make stack-demo  # two orbits, screened, with a summary of what was found
```

`docker compose --profile demo up -d sim` runs the simulator continuously
instead. `make down` stops everything and keeps the data; `make logs` follows
all three processes.

## Quick start — local checkout

```bash
cp .env.example .env
docker compose up -d db          # PostgreSQL on localhost:5433
uv venv --python 3.12 && uv pip install -e '.[ml,dev]'
source .venv/bin/activate
alembic upgrade head             # create the three tables
```

Then run the three processes, each in its own terminal:

```bash
mgs-api                          # http://localhost:8000/docs
mgs-worker                       # screens telemetry, writes alerts
mgs-sim --duration 24h           # flies a day of real passes, compressed
```

Then open **<http://localhost:8000/>** — the dashboard: live battery, temperature
and signal charts with the worker's own limits drawn on them, the passes it
heard, and the open alerts with an acknowledge button. Or from the shell:

```bash
curl -s localhost:8000/api/v1/passes | jq
curl -s 'localhost:8000/api/v1/alerts?open_only=true' | jq
curl -s 'localhost:8000/api/v1/telemetry/series?buckets=20' | jq
```

`make demo` does all of it in one shot.

## The orbit is real

A single ground station sees a low-Earth satellite for about **1% of the day**.
That is the fact the whole design turns on, and it is the one a toy circular
orbit cannot produce — the first version of this simulator had to shift the
ground track over the station to manufacture a pass every orbit, which quietly
deleted the problem.

Now positions come from SGP4 against a published element set, and a demo day
over Hanoi looks like this:

```
predicted pass  AOS 10:27:55   1.9 min  max elevation  5.5°
predicted pass  AOS 12:01:06   8.4 min  max elevation 55.7°
predicted pass  AOS 20:15:53   6.4 min  max elevation 13.8°
predicted pass  AOS 21:51:58   7.8 min  max elevation 27.0°
```

Four windows, two clusters, thirteen hours of silence in the middle — and pass
quality decides how much data comes down. In that run the 1.9-minute graze
cleared 235 frames off the recorder; the 8.4-minute overhead pass cleared 733.
When a silence outlasts the recorder, the oldest frames are overwritten and
show up on the ground as a `DATA_GAP`.

Everything geometric follows from the propagation: slant range, azimuth and
elevation, range rate, Doppler shift (±9 kHz at 437 MHz), eclipse, and received
power from free-space path loss. Reasoning:
[`decisions/0004`](docs/decisions/0004-real-orbit-propagation.md).

### Element sets

The repository ships a real ISS element set so the simulator, the tests, and CI
all run offline. It ages — SGP4 drifts about a kilometre a day from its epoch —
and the simulator says so when it is more than a fortnight old.

```bash
mgs-sim --fetch-tle                     # a current element set from Celestrak
mgs-sim --catalog-number 43013          # some other satellite
mgs-sim --tle-file ./mysat.tle          # your own
mgs-sim --station-lat 10.82 --station-lon 106.63 --station-id SAIGON-GS
```

## The four tables

- **`passes`** — one contact window, AOS to LOS. A partial unique index makes
  "at most one open pass per satellite" a database guarantee, so attaching a
  frame to the current pass is never ambiguous.
- **`telemetry`** — one downlinked frame, append-only. `UNIQUE (satellite_id,
  seq)` makes ingestion idempotent: a retried POST returns the original row.
  `screened_at` is the worker's cursor, which is why there is no fourth
  bookkeeping table.
- **`alerts`** — one detected problem, with a `dedupe_key` unique index, so
- **`satellites`** — the spacecraft the station tracks. Rows register
  themselves on first contact, and carry nullable limit overrides: `NULL` means
  the station default, so an unedited row screens exactly as before. This is
  where "6.5 V is fine on the old bird, alarming on the new one" lives.
  re-running the worker over the same frames never duplicates an alert.

Full reasoning: [`docs/product/schema.md`](docs/product/schema.md).

## What the worker detects

| Rule | Trigger |
|---|---|
| `BATTERY_LOW` | voltage below `MGS_BATTERY_MIN_V`, critical below `MGS_BATTERY_CRITICAL_V` |
| `TEMP_HIGH` / `TEMP_LOW` | outside `MGS_TEMP_MIN_C … MGS_TEMP_MAX_C` |
| `SAFE_MODE` | spacecraft reported SAFE mode |
| `DATA_GAP` | on-board sequence numbers jumped — frames lost on the downlink |
| `ML_OUTLIER` | IsolationForest over a rolling window, z-score while cold-starting |

An alert describes an *episode*, not a frame, and a condition has to stay quiet
for `MGS_ALERT_CLEAR_AFTER_SECONDS` before its alert resolves — one normal
frame in the middle of an excursion is not the end of it. Before that quiet
period existed, `ML_OUTLIER` reopened eleven times in a day of real telemetry,
seven of them within five minutes, the shortest gap being a single frame.

The threshold rules need no dependencies. The statistical layer needs the `ml`
extra; without it the worker degrades to z-score rather than failing.

## The dashboard

One page at `/`, served by the same FastAPI app, no build step and no
dependencies to install. Three separate charts rather than one with three
y-axes: volts, degrees and dBm share no scale, and overlaying them would invent
a correlation that is not in the data.

- Threshold lines come from `GET /api/v1/summary`, which reports the worker's
  live limits — a line on a chart can never disagree with the rule that pages
  someone.
- Shaded bands mark the passes, so you can see the signal strength rise as the
  satellite climbs above the horizon.
- Charts are drawn from `GET /api/v1/telemetry/series`, which buckets and
  aggregates **in PostgreSQL**. A 900-pixel chart has no use for 50,000 rows.
- Every chart has a table view (the "Table view" button), a crosshair tooltip,
  and keyboard navigation with the arrow keys.

## Operating it

**Writes need a key.** Ingest, pass management, and acknowledging an alert all
require `X-API-Key`; reading does not.

```bash
MGS_API_KEYS=a-real-key mgs-api
mgs-sim --api-key a-real-key
curl -s localhost:8000/health | jq .auth      # "enabled" or "disabled"
```

Leaving `MGS_API_KEYS` empty leaves every write open — the API warns at startup
and `/health` says so. The compose stack sets a key by default. This is a shared
secret, not per-operator identity: see
[`decisions/0005`](docs/decisions/0005-api-keys-guard-writes.md) for what that
does and does not buy.

**Telemetry does not delete itself.** About 2.3 GB per satellite-year at the
default cadence, seventy at one frame a second.

```bash
mgs-retention --days 90 --dry-run     # what would go
mgs-retention --days 90               # actually go
docker compose --profile maintenance run --rm retention
```

Frames an alert cites are evidence and are never deleted, and neither are
frames the worker has not screened yet. It deletes rather than partitions
because partitioning by time would force `UNIQUE (satellite_id, seq)` to widen
and take idempotent ingestion with it —
[`decisions/0006`](docs/decisions/0006-retention-not-partitioning.md) has the
error message from PostgreSQL saying so.

## Tests and CI

```bash
make test                        # 138 tests; the integration ones need `make db-up`
make lint                        # ruff check + format --check
```

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs three jobs on every
push and pull request:

| Job | What it proves |
|---|---|
| **Lint** | `ruff check` and `ruff format --check` over `src` and `tests` |
| **Tests** | the whole suite against a real PostgreSQL service |
| **Container stack** | builds the image, brings the stack up, flies two orbits, screens them, and asserts frames, passes and alerts actually landed — then re-screens the same frames and asserts no alert was duplicated |

The third job is the one that earns its keep: it caught a crash and then a
duplicate-alert bug on re-screening that the unit tests missed, because both
only appear once an episode has been closed and replayed.

Dependencies are pinned in `uv.lock`, and both CI and the image install with
`uv sync --frozen`, so a build resolves nothing.

## Agent tooling

This project was written in a working session with
[Claude Code](https://claude.com/claude-code), and the tooling from that
session is kept in the repository rather than swept out of it:
[repository-harness](https://github.com/hoangnb24/repository-harness)
(`AGENTS.md`, `docs/WORKFLOW.md`, `.agents/skills/`) supplies the repository
protocol, and a project-scoped
[graphify](https://github.com/Graphify-Labs/graphify) skill builds a knowledge
graph to query instead of grepping. See [`docs/agents.md`](docs/agents.md).

The tooling is not the interesting part. The record is: six decision documents
under `docs/decisions/` say which alternative was rejected and why, with
measurements either side of the change, and the commit history says which bugs
the container checks found before anyone else could.

## License

MIT — see [`LICENSE`](LICENSE).
