"""営業日報メモ（試作版）

話す → 文字起こし → AIが「訪問先・面談者・商談内容・次回アポ・ToDo」に整理 → テキスト/CSVで保存
"""

import copy
import csv
import io
from datetime import date, datetime, timedelta, timezone

import streamlit as st

JST = timezone(timedelta(hours=9))
CLAUDE_MODEL = "claude-sonnet-5-5"
OPENAI_MODEL = "gpt-4.1-mini"  # OpenAIで日報に整理するときのモデル
TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"  # 文字起こし（うまくいかなければ "whisper-1"）

st.set_page_config(page_title="営業日報メモ", page_icon="📝", layout="centered")

# 録音まわりのボタン（マイク・停止・再生）を大きくして、押しやすくする
st.markdown(
    """
    <style>
    [data-testid="stAudioInput"] [data-testid="stAudioInputActionButton"] {
        min-width: 64px;
        min-height: 64px;
        border-radius: 50%;
        background-color: #ff4b4b;
        color: #ffffff;
    }
    [data-testid="stAudioInput"] [data-testid="stAudioInputActionButton"]:hover {
        background-color: #e03c3c;
        color: #ffffff;
    }
    [data-testid="stAudioInput"] [data-testid="stAudioInputActionButton"] svg {
        width: 36px !important;
        height: 36px !important;
    }
    [data-testid="stAudioInput"] [data-testid="stAudioInputActionButton"] span {
        font-size: 36px !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------- APIキー ----------
def get_key(name: str) -> str:
    """secrets.toml にあればそれを使い、なければサイドバーの入力欄の値を使う"""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return st.session_state.get(f"input_{name}", "")


with st.sidebar:
    st.header("設定")
    st.text_input(
        "OpenAI APIキー",
        type="password",
        key="input_OPENAI_API_KEY",
        help="これ1つで、文字起こしと日報への整理の両方ができます。",
    )
    st.text_input(
        "Anthropic APIキー（任意）",
        type="password",
        key="input_ANTHROPIC_API_KEY",
        help="入れると、日報への整理にClaudeを使います。",
    )
    st.caption("どちらか1つあれば動きます。secrets.toml に設定済みなら入力不要です。")


# ---------- 状態の初期化 ----------
ss = st.session_state
ss.setdefault("transcript", "")
ss.setdefault("report", None)
ss.setdefault("history", [])


# ---------- AI処理 ----------
def transcribe(audio_bytes: bytes) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=get_key("OPENAI_API_KEY"))
    result = client.audio.transcriptions.create(
        model=TRANSCRIBE_MODEL,
        file=("recording.wav", audio_bytes),
        language="ja",
    )
    return result.text


REPORT_TOOL = {
    "name": "save_sales_report",
    "description": "商談メモの内容を営業日報の項目に整理して保存する",
    "input_schema": {
        "type": "object",
        "properties": {
            "visit_to": {"type": "string", "description": "訪問先（会社名・部署など）。不明なら『未記載』"},
            "attendees": {"type": "string", "description": "面談者（氏名・役職）。複数なら『、』区切り。不明なら『未記載』"},
            "summary": {
                "type": "array",
                "items": {"type": "string"},
                "description": "商談内容の要約。1項目1文で3〜5項目程度",
            },
            "next_appointment": {
                "type": "string",
                "description": "次回アポの日時と内容（例：2026-11-04（火）14:00 見積提示）。決まっていなければ『未定』",
            },
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": "やること"},
                        "due": {"type": "string", "description": "期限（YYYY-MM-DD）。不明なら空文字"},
                    },
                    "required": ["task", "due"],
                },
                "description": "次のアクション（自分がやるべきこと）",
            },
        },
        "required": ["visit_to", "attendees", "summary", "next_appointment", "todos"],
    },
}


def build_prompt(memo: str, visit_date: date) -> str:
    weekday = "月火水木金土日"[visit_date.weekday()]
    return f"""あなたはB2B営業の日報作成アシスタントです。
以下は営業担当者が商談後に話した（または書いた）メモです。save_sales_report ツールで日報の項目に整理してください。

ルール:
- 訪問日は {visit_date.isoformat()}（{weekday}）です。「来週火曜」「明後日」などの相対的な日付は、この日を基準に具体的な日付（YYYY-MM-DD）に直してください。
- メモに書かれていないことは推測で作らず、「未記載」「未定」としてください。
- 商談内容の要約は、上司が読んで状況がつかめるよう、事実ベースで簡潔に。
- ToDoは「誰が見ても次に何をすればいいか分かる」具体的な動詞で書いてください。
- 話し言葉の言い直しや「えーと」などは除いてください。

メモ:
{memo}"""


def structure_report(memo: str, visit_date: date) -> dict:
    """Anthropicキーがあれば Claude、なければ OpenAI で整理する"""
    if get_key("ANTHROPIC_API_KEY"):
        return structure_with_claude(memo, visit_date)
    return structure_with_openai(memo, visit_date)


def structure_with_openai(memo: str, visit_date: date) -> dict:
    import json

    from openai import OpenAI

    client = OpenAI(api_key=get_key("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": REPORT_TOOL["name"],
                    "description": REPORT_TOOL["description"],
                    "parameters": REPORT_TOOL["input_schema"],
                },
            }
        ],
        tool_choice={"type": "function", "function": {"name": REPORT_TOOL["name"]}},
        messages=[{"role": "user", "content": build_prompt(memo, visit_date)}],
    )
    calls = response.choices[0].message.tool_calls
    if not calls:
        raise RuntimeError("AIから日報データを受け取れませんでした。")
    return json.loads(calls[0].function.arguments)


def structure_with_claude(memo: str, visit_date: date) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=get_key("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        tools=[REPORT_TOOL],
        tool_choice={"type": "tool", "name": REPORT_TOOL["name"]},
        messages=[{"role": "user", "content": build_prompt(memo, visit_date)}],
    )
    for block in response.content:
        if block.type == "tool_use":
            return dict(block.input)
    raise RuntimeError("AIから日報データを受け取れませんでした。")


# ---------- 出力の整形 ----------
def report_to_text(r: dict) -> str:
    lines = [
        f"【営業日報】{r['date']}",
        f"訪問先：{r['visit_to']}",
        f"面談者：{r['attendees']}",
        "",
        "■ 商談内容",
        *[f"・{s}" for s in r["summary"]],
        "",
        f"■ 次回アポ：{r['next_appointment']}",
        "",
        "■ ToDo",
    ]
    if r["todos"]:
        lines += [f"□ {t['task']}" + (f"（期限：{t['due']}）" if t["due"] else "") for t in r["todos"]]
    else:
        lines.append("（なし）")
    return "\n".join(lines)


CSV_COLUMNS = ["日付", "訪問先", "面談者", "商談内容", "次回アポ", "ToDo", "日報本文"]


def history_to_csv(history: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for r in history:
        writer.writerow(
            [
                r["date"],
                r["visit_to"],
                r["attendees"],
                "\n".join(r["summary"]),
                r["next_appointment"],
                "\n".join(f"{t['task']}（{t['due']}）" if t["due"] else t["task"] for t in r["todos"]),
                r.get("final_text") or report_to_text(r),
            ]
        )
    # Excelで文字化けしないようBOM付きUTF-8
    return buf.getvalue().encode("utf-8-sig")


# ---------- 画面用の小道具 ----------
def load_report(data: dict, editing_index=None) -> None:
    """日報を③④の編集欄に読み込む（新しく作ったとき・一覧から修正するとき）"""
    ss.report = copy.deepcopy(data)
    ss.ver += 1  # 入力欄を新しい内容で作り直すための番号
    ss.editing_index = editing_index


ss.setdefault("ver", 0)
ss.setdefault("editing_index", None)
ss.setdefault("flash", "")


# ---------- デモ用サンプル（APIキーなしで動きを見せるため） ----------
SAMPLE_MEMO = (
    "えーと、今日はサンプル工業の製造部の山本部長と、情報システム課の井上さんと面談しました。"
    "今は日報を紙とExcelで管理していて、集計に毎月2日くらいかかっているのが課題だそうです。"
    "新しいシステムの導入を検討中で、予算は来期に取る予定とのこと。競合はもう1社が提案しているみたいです。"
    "来週火曜の14時に見積を持っていくことになりました。それまでに同じ業種の導入事例を2社分そろえます。"
)


def sample_report(visit_date: date) -> dict:
    """サンプルメモをAIが整理した結果の例（架空の会社・人物です）"""
    next_tue = visit_date + timedelta(days=7 - visit_date.weekday() + 1)
    prep_due = next_tue - timedelta(days=1)
    return {
        "visit_to": "サンプル工業株式会社 製造部（架空）",
        "attendees": "山本部長、情報システム課 井上様",
        "summary": [
            "日報を紙とExcelで管理しており、月次の集計に約2日かかっている点が課題。",
            "新システムの導入を検討中で、予算は来期に確保する予定。",
            "競合1社がすでに提案している。",
            "次回訪問で見積を提示することで合意。",
        ],
        "next_appointment": f"{next_tue.isoformat()}（火）14:00 見積提示",
        "todos": [
            {"task": "見積書を作成する", "due": prep_due.isoformat()},
            {"task": "同業種の導入事例を2社分まとめる", "due": prep_due.isoformat()},
        ],
    }


# ---------- 画面 ----------
st.title("📝 営業日報メモ")
st.caption("商談のあとに話すだけで、日報の形に整理します。")

visit_date = st.date_input("訪問日", value=datetime.now(JST).date())

# 1. 話す
st.subheader("① 商談内容を話す")
st.caption("例：「〇〇商事の田中部長と面談。新システムの導入を検討中で、予算は来期。来週火曜の14時に見積を持っていく。それまでに導入事例を2社分そろえる。」")

audio = st.audio_input("録音する（赤い丸のマイクを押すと開始、もう一度押すと停止）")
if audio is not None:
    if st.button("🎙️ 文字起こしする", width="stretch"):
        if not get_key("OPENAI_API_KEY"):
            st.warning("文字起こしにはOpenAI APIキーが必要です。サイドバーで設定するか、下の欄にキーボードの音声入力で入れてください。")
        else:
            with st.spinner("文字起こし中…"):
                try:
                    ss.transcript = transcribe(audio.getvalue())
                    ss.memo_area = ss.transcript
                except Exception as e:
                    st.error(f"文字起こしに失敗しました：{e}")

if st.button(
    "🧪 サンプルで試す（APIキー不要）",
    width="stretch",
    help="架空の商談メモと、AIが整理した結果の例を表示します。APIは使わないので料金はかかりません。",
):
    ss.memo_area = SAMPLE_MEMO
    _sample = sample_report(visit_date)
    _sample["date"] = visit_date.isoformat()
    load_report(_sample)
    ss.flash = "サンプルを表示しています（架空の会社・人物です）。下の③④で、修正や保存の流れを試せます。"

memo = st.text_area(
    "メモ（文字起こし結果。直接入力・修正もできます）",
    key="memo_area",
    height=160,
    placeholder="ここに文字起こしが入ります。スマホのキーボードのマイクボタンで話して入力してもOKです。",
)

# 2. 整理する
st.subheader("② AIで日報に整理")
if st.button("✨ 日報にまとめる", type="primary", width="stretch", disabled=not memo.strip()):
    if not (get_key("OPENAI_API_KEY") or get_key("ANTHROPIC_API_KEY")):
        st.warning("サイドバーでOpenAI APIキーを設定してください。")
    else:
        with st.spinner("整理中…"):
            try:
                data = structure_report(memo, visit_date)
                data["date"] = visit_date.isoformat()
                load_report(data)
            except Exception as e:
                st.error(f"整理に失敗しました：{e}")

if ss.report:
    r = ss.report
    v = ss.ver
    editing = ss.editing_index is not None

    # 3. 項目ごとに直す
    st.subheader("③ ✏️ 項目ごとに直す")
    if ss.flash:
        st.success(ss.flash)
        ss.flash = ""
    if editing:
        st.warning(f"一覧に保存済みの日報（{r['date']}　{r['visit_to']}）を修正中です。")
    st.info("AIのまとめが違っていたら、ここで直してください。**枠の中はすべて書き換えられます。**直した内容は④に自動で反映されます。")

    r["visit_to"] = st.text_input("訪問先", r["visit_to"], key=f"visit_to_{v}")
    r["attendees"] = st.text_input("面談者", r["attendees"], key=f"attendees_{v}")
    summary_text = st.text_area(
        "商談内容（1行に1項目。行を足す・消すのも自由です）",
        "\n".join(r["summary"]),
        height=140,
        key=f"summary_{v}",
    )
    r["summary"] = [s.strip() for s in summary_text.splitlines() if s.strip()]
    r["next_appointment"] = st.text_input("次回アポ", r["next_appointment"], key=f"next_{v}")

    st.markdown("**ToDo**")
    st.caption("マスをダブルクリックで編集 ／ 一番下の空の行に書くと追加 ／ 行の左端を選んでゴミ箱アイコンで削除")
    todo_rows = st.data_editor(
        [{"やること": t["task"], "期限": t["due"]} for t in r["todos"]] or [{"やること": "", "期限": ""}],
        num_rows="dynamic",
        width="stretch",
        key=f"todo_{v}",
    )
    r["todos"] = [
        {"task": row["やること"], "due": row.get("期限") or ""}
        for row in todo_rows
        if row.get("やること")
    ]

    # 4. 文章で仕上げる
    st.subheader("④ ✏️ 文章で仕上げて保存")
    st.info("この欄も**自由に書き直せます**。言い回しの調整や、ひとこと追記はここで。")
    st.caption("※④を書き直した後に③を直すと、④は③の内容で作り直されます。③ → ④ の順で直すのがおすすめです。")

    generated = report_to_text(r)
    # ③が変わっていなければ、④で書き直した文章を残す
    default_text = r["final_text"] if r.get("final_base") == generated else generated
    final_text = st.text_area(
        "日報（そのまま送れる文章）",
        default_text,
        height=320,
        key=f"final_{v}_{hash(generated)}",
    )
    r["final_text"] = final_text
    r["final_base"] = generated

    with st.expander("📋 コピー用に表示（右上のアイコンでコピー）"):
        st.code(final_text, language=None)

    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            "📄 テキストで保存",
            final_text.encode("utf-8"),
            file_name=f"日報_{r['date']}_{r['visit_to']}.txt",
            mime="text/plain",
            width="stretch",
        )
    with col2:
        if editing:
            if st.button("💾 一覧の内容を更新", type="primary", width="stretch"):
                ss.history[ss.editing_index] = copy.deepcopy(r)
                st.success("一覧の日報を更新しました。")
        else:
            if st.button("➕ 一覧に追加", type="primary", width="stretch"):
                ss.history.append(copy.deepcopy(r))
                ss.editing_index = len(ss.history) - 1  # 2回押しても重複しないよう、以後は「更新」に
                st.success("一覧に追加しました。このあと直した場合は「一覧の内容を更新」を押してください。")
                st.rerun()

# 一覧
if ss.history:
    st.divider()
    st.subheader(f"📚 一覧（{len(ss.history)}件）")
    for i in reversed(range(len(ss.history))):
        h = ss.history[i]
        mark = "（修正中）" if ss.report and ss.editing_index == i else ""
        with st.expander(f"{h['date']}　{h['visit_to']}{mark}"):
            st.text(h.get("final_text") or report_to_text(h))
            if st.button("✏️ この日報を修正する", key=f"edit_{i}", width="stretch"):
                load_report(h, editing_index=i)
                ss.flash = "修正したい日報を③④に読み込みました。上にスクロールして直してください。"
                st.rerun()
    st.download_button(
        "📊 一覧をCSVで保存（Excelで開けます）",
        history_to_csv(ss.history),
        file_name=f"日報一覧_{datetime.now(JST):%Y%m%d}.csv",
        mime="text/csv",
        width="stretch",
    )
    st.caption("※試作版のため、一覧はページを閉じると消えます。CSVで保存しておいてください。")
