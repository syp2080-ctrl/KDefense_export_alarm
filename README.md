# 🛡️ 방산 수출 계약 뉴스 → 텔레그램 알림

한국 방산 수출 계약 **체결·서명·수주** 기사가 나오면 요약 + 원문 링크를 텔레그램으로 보내 줍니다.

```
🛡️ 방산 수출 계약 소식

한화에어로스페이스, ○○국과 K9 자주포 2차 이행계약 체결

🌍 수입국: ○○
🏢 업체: 한화에어로스페이스
📦 품목: K9 자주포
💰 규모: 약 ○조원

📝 (Claude가 쓴 3문장 요약)

🔗 원문 보기 · 언론사 11/17 09:12
```

## 동작 방식
1. **수집** – 네이버 뉴스 검색 API(키가 있을 때) + Google 뉴스 RSS에서 `QUERIES` 검색어로 최신 기사를 모읍니다.
2. **1차 필터** – 방산 용어와 계약 신호(계약·체결·수주·서명)가 있어야 통과합니다. "전망·추진·협상·가능성" 같은 추측성 제목은 걸러냅니다.
3. **2차 판정·요약** – Claude API가 "실제 수출 계약 체결 기사인지" 판정하고 수입국·업체·품목·규모와 3문장 요약을 만듭니다.
4. **중복 제거** – 같은 URL, 비슷한 제목, 같은 (수입국·업체·품목) 계약은 72시간 동안 한 번만 보냅니다.
5. **발송** – 텔레그램 봇으로 보냅니다.

> 첫 실행 때는 이미 나와 있던 기사를 보내지 않고 "모니터링 시작" 메시지만 보냅니다. 이후 새 기사부터 알림이 옵니다. (`--send-existing`으로 바꿀 수 있음)

---

## 1. 준비물 (키 발급)

| 항목 | 필수 | 발급 방법 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | 텔레그램에서 **@BotFather** → `/newbot` → 이름 정하기 → 토큰 복사 |
| `TELEGRAM_CHAT_ID` | ✅ | 만든 봇에게 아무 메시지나 보낸 뒤 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates` 열기 → `"chat":{"id": 123456789}` 숫자. 그룹/채널에 받으려면 봇을 그곳에 초대(채널은 관리자로) 후 같은 방법으로 확인 (`-100…` 형식) |
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 권장 | [developers.naver.com](https://developers.naver.com) → 애플리케이션 등록 → 사용 API **검색** 선택 → Client ID/Secret 복사 (무료, 하루 25,000회) |
| `ANTHROPIC_API_KEY` | 권장 | [console.anthropic.com](https://console.anthropic.com) → API Keys. 없으면 키워드 규칙만으로 판정하고, 요약 대신 기사 미리보기 문구를 보냅니다 |

- 네이버 키가 없으면 Google 뉴스 RSS만 씁니다. 이때 원문 링크는 Google 뉴스를 거쳐 언론사로 이동합니다.
- 비용 참고: Claude는 1차 필터를 통과한 기사만 분석하므로 보통 하루 수십 건 이하입니다. 기본 모델은 `claude-haiku-5-5`(저렴·빠름)이고 `CLAUDE_MODEL` 환경변수로 바꿀 수 있습니다.

## 2-A. GitHub Actions로 운영 (서버 없이, 추천)

1. GitHub에 새 저장소를 만들고 이 폴더의 파일을 그대로 올립니다. (`.github/workflows/monitor.yml` 경로 유지)
   - 비공개(Private) 저장소를 권장합니다. 비공개라도 무료 사용량(월 2,000분) 안에서 돌아갑니다. 10분 간격이면 월 약 4,300회 × 30초 정도라 빠듯할 수 있으니, 초과하면 `cron`을 `*/15`로 늘리거나 공개 저장소로 쓰세요(공개 저장소는 무제한).
2. 저장소 **Settings → Secrets and variables → Actions → New repository secret**에서 위 키 5개를 등록합니다.
3. **Actions** 탭 → "방산 수출 뉴스 모니터" → **Run workflow**로 한 번 실행 → 텔레그램에 "모니터링을 시작합니다" 메시지가 오면 성공입니다.
4. 이후 10분마다 자동으로 실행됩니다. 처리한 기사 기록은 `seen.json`에 커밋됩니다.

> GitHub 예약 실행은 몇 분씩 밀릴 수 있습니다. 더 빠른 알림이 필요하면 2-B 방식을 쓰세요.

## 2-B. 내 PC/서버에서 상시 실행

```bash
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
export NAVER_CLIENT_ID="..."
export NAVER_CLIENT_SECRET="..."
export ANTHROPIC_API_KEY="..."

python monitor.py --test        # 텔레그램 연결 확인
python monitor.py --loop 180    # 3분마다 확인 (Ctrl+C로 종료)
```

Windows PowerShell은 `export` 대신 `$env:TELEGRAM_BOT_TOKEN="..."` 형식으로 설정합니다.
리눅스 서버라면 `nohup python monitor.py --loop 180 &` 또는 systemd/cron으로 띄우면 됩니다.

## 3. 조정하기

`monitor.py` 상단에서 바꿀 수 있습니다.

- `QUERIES` – 검색어 (새 무기체계·업체 추가)
- `DEFENSE_TERMS` / `CONTRACT_TERMS` / `SPECULATIVE_TERMS` – 1차 필터 키워드
- `PROMPT` – Claude 판정 기준 (예: MOU도 받고 싶으면 문구 수정)

환경변수: `MAX_AGE_HOURS`(기본 24), `TITLE_SIMILARITY`(0.6), `DEAL_DEDUP_HOURS`(72), `MAX_ANALYZE_PER_RUN`(15), `CLAUDE_MODEL`.

## 문제 해결
- **알림이 안 옴** → `python monitor.py -v`로 로그 확인. "1차 필터 통과 0건"이면 실제로 새 계약 기사가 없거나 키워드가 너무 좁은 것입니다.
- **같은 소식이 여러 번 옴** → `TITLE_SIMILARITY`를 0.5로 낮추세요.
- **처음부터 다시** → `seen.json`을 지우면 첫 실행 상태로 돌아갑니다.
