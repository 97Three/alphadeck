# AlphaDeck

**浠庣爺绌堕棶棰橈紝鍒板彲浜や粯鐨勯噺鍖栫湅鏉裤€?*

AlphaDeck 鏄竴涓湰鍦拌繍琛岀殑閲忓寲鐮旂┒宸ヤ綔鍙般€傜敤鑷劧璇█鎻愬嚭鐮旂┒闂锛孉gent 璐熻矗鍙栨暟銆佽绠楀拰鐢熸垚锛屾渶缁堜氦浠樹竴涓彲棰勮銆佸彲楠屾敹鐨?Vue 鐪嬫澘锛涚爺绌惰繃绋嬨€佽瘉鎹拰浜х墿閮界暀鍦ㄦ湰椤圭洰鍐呫€?
鍓嶇鏄?Vue 3 + Vite + TypeScript锛岀爺绌朵笌琛屾儏鑳藉姏鐢变袱涓?Python FastAPI 鏈嶅姟鎻愪緵锛屾椂搴忔暟鎹惤鍦?TimescaleDB锛岀煭鏈熺紦瀛樻斁鍦?Redis銆?
## 鑳藉仛浠€涔?
- **浠诲姟杞ㄩ亾寮忕爺绌?*锛氬乏渚у垏鎹㈢爺绌朵换鍔★紝鍙充晶閫夋嫨妯″瀷涓庤緭鍑烘柟寮忥紝瀹炴椂璺熻釜 Agent 杩囩▼銆佽瘉鎹€佹枃浠朵笌鐢熸垚缁撴灉銆?- **琛屾儏涓庡熀鏈潰**锛氳偂绁?/ ETF 瀹囧畽銆佹棩绾夸笌瀹炴椂蹇収銆佽储鍔℃姤鍛婄増鏈€佽ˉ鏁颁笌鏁版嵁璐ㄩ噺妫€鏌ャ€?- **绛栫暐涓庡洖娴?*锛氬洜瀛愮洰褰曘€佺瓥鐣ユ壂鎻忋€佸洖娴嬫墽琛屼笌缁撴灉鐣欐。銆?- **鐪嬫澘鐢熸垚**锛氭瘡涓」鐩敓鎴愮嫭绔嬬殑 Vue 鐪嬫澘锛屾敮鎸侀瑙堛€佹瀯寤轰笌楠屾敹銆?- **娌荤悊涓庤繍缁?*锛氭潈闄愩€佸璁°€佽瘎娴嬨€丼kills 绠＄悊銆佽繍琛屾不鐞嗕笌璁剧疆銆?- **鏈湴浼樺厛**锛氬崟鏈?Docker Compose锛屾暟鎹€佹ā鍨?Key 涓庝骇鐗╅兘鐣欏湪鏈満銆?
## 鏋舵瀯

```mermaid
flowchart LR
    B[Vue 3 宸ヤ綔鍙?br/>:3200] --> P[Platform API<br/>:38084]
    P --> DB[(TimescaleDB<br/>:35437)]
    P --> R[(Redis<br/>:36384)]
    P --> M[Market Data API<br/>:8103]
    M --> S[琛屾儏鏁版嵁婧怾
    P --> W[鐢熸垚 / 璇勬祴 Worker]
    W --> B
```

- **Platform API**锛氶」鐩€丄gent銆佹潈闄愩€佽瘎娴嬨€丼kills銆佺瓥鐣ヤ笌鎶ュ憡銆?- **Market Data API**锛氳鎯呫€佽ˉ鏁般€佸洜瀛愪笌鍥炴祴鏁版嵁銆?- **Worker**锛氱湅鏉跨敓鎴愪笌璇勬祴浠诲姟锛屽拰 API 鍏辩敤鍚屼竴濂楁暟鎹笌濂戠害銆?- **Vue 宸ヤ綔鍙?*锛氭墍鏈夐〉闈㈤€氳繃 `/api` 璁块棶 Platform API锛涚敓浜ф瀯寤哄悗涔熷彲鐢?Platform API 鐩存帴鎵樼銆?
## 蹇€熷紑濮?
寤鸿鍦?**WSL2 / Linux** 涓嬭繍琛岋細鐪嬫澘鐢熸垚涓庨瑙堥渶瑕?Linux 闅旂鑳藉姏锛孌ocker Desktop 闇€瑕佸紑鍚搴斿彂琛岀増鐨勯泦鎴愩€?
鍓嶇疆锛歂ode.js 22.19+銆乶pm 10+銆丳ython 3.14銆乕uv](https://docs.astral.sh/uv/)銆丏ocker Compose銆?
```bash
npm run setup
npm run db:up
npm run db:init
npm run dev
```

鎵撳紑 <http://127.0.0.1:3200>銆?
`npm run dev` 浼氬悓鏃跺惎鍔?Vue銆丳latform API銆丮arket Data API銆佺敓鎴?Worker 鍜岃瘎娴?Worker锛涙寜 `Ctrl+C` 鍋滄搴旂敤杩涚▼銆傛暟鎹簱鍜?Redis 浼氫繚鐣欏湪 Docker 涓紝闇€瑕佸仠姝㈡椂鎵ц `npm run db:down`銆?
妯″瀷 Key 鍙斁鍦?`.env.local`锛屼緥濡傦細

```bash
DEEPSEEK_API_KEY="..."
```

淇濆瓨鍚庨噸鍚?`npm run dev`锛屽啀鍒般€岃缃€嶆鏌ヨ繛鎺ャ€?*涓嶈鎻愪氦 `.env.local`銆?*

## 鏈嶅姟涓庣鍙?
| 閮ㄥ垎 | 鍦板潃 / 鐩綍 | 鐢ㄩ€?|
| --- | --- | --- |
| Vue 宸ヤ綔鍙?| `127.0.0.1:3200`锛宍web/` | 椤甸潰銆佷换鍔¤建閬撲笌鍚勫姛鑳藉叆鍙?|
| Platform API | `127.0.0.1:38084`锛宍services/platform-api/` | 椤圭洰銆丄gent銆佹潈闄愩€佽瘎娴嬨€丼kills銆佷氦浠?|
| Market Data API | `127.0.0.1:8103`锛宍services/market-data/` | 琛屾儏銆佽ˉ鏁般€佺瓥鐣ヤ笌鍥炴祴 |
| TimescaleDB | `127.0.0.1:35437` | 涓氬姟涓庢椂搴忔暟鎹?|
| Redis | `127.0.0.1:36384` | 鍙涪澶辩殑鐭湡缂撳瓨 |
| 鐪嬫澘妯℃澘 | `config/vue-dashboard-template/` | 鏂伴」鐩殑 Vue / Vite 妯℃澘 |

## 甯哥敤鍛戒护

```bash
npm run dev                  # 鍚姩鍏ㄩ儴鏈嶅姟
npm run check:standalone-runtime
npm run check:skills
npm run check:integrations
npm run db:up / db:down      # 鍚仠鏁版嵁搴撲笌 Redis
npm run obs:up / obs:down    # 鍙€夛細Loki + Grafana + Alloy
```

## 鐩綍缁撴瀯

```text
web/                         Vue 3 + Vite + TypeScript 宸ヤ綔鍙?services/platform-api/       FastAPI 骞冲彴鏈嶅姟涓?Agent
services/market-data/        FastAPI 琛屾儏鏈嶅姟
config/                      鐪嬫澘妯℃澘涓庡绾﹂厤缃?prisma/                      Prisma schema 涓庤縼绉?sqls/                        TimescaleDB 鍒濆鍖栦笌澧為噺 SQL
deploy/                      systemd 涓庨儴缃查厤缃?docs/                        鏋舵瀯銆丄PI銆侀厤缃笌杩愮淮鏂囨。
scripts/                     寮€鍙戙€佹瀯寤轰笌妫€鏌ヨ剼鏈?benchmarks/                  鍩哄噯涓庤瘎娴嬫暟鎹?```

## 閰嶇疆涓庡畨鍏?
- 鎵€鏈夐厤缃€氳繃鐜鍙橀噺娉ㄥ叆锛岀ず渚嬭 `.env.example` 涓?`.env.production.example`銆?- `.env`銆乣.env.local`銆乣data/`銆乣tmp/`銆乣migration-reference/` 绛夊潎宸插湪 `.gitignore` 涓帓闄ゃ€?- 鐪熷疄瀵嗛挜鍙斁 `.env.local` 鎴栬繘绋嬬幆澧冿紝涓嶈繘鍏ヤ粨搴撱€?- 宸ヤ綔鍙伴粯璁ゅ彧鐩戝惉鏈満鍦板潃锛屼笉瑕佺洿鎺ユ毚闇插埌鍏綉銆?- 鐢熶骇閮ㄧ讲鍓嶈闃呰 `docs/operations-runbook.md` 涓?`docs/release-runbook.md`銆?
## 杈圭晫

杩欐槸鏈湴銆佸崟鐢ㄦ埛鐨勯噺鍖栫爺绌跺伐浣滃彴锛屼笉鏄绉熸埛 SaaS锛氫笉鎻愪緵鍏綉绾ц韩浠戒綋绯汇€佽法绉熸埛闅旂鎴栨墭绠″紡杩愮淮銆傜敓鎴愮湅鏉垮拰璇勬祴浠诲姟渚濊禆鏈満璧勬簮锛岄暱鏃堕棿鎴栧ぇ瑙勬ā浠诲姟闇€瑕佽嚜琛屾墿灞?Worker銆?
## 璁稿彲璇?
[MIT](LICENSE)
