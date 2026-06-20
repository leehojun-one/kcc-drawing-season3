# -*- coding: utf-8 -*-
"""
manual_form.py  —  수기 실측 입력 폼 (무료 수동 입력 모듈)
=========================================================
견적서 49개 분석으로 확정된 드롭다운 목록을 사용하여,
직원이 수기 실측지를 보고 직접 입력 → 기존 도면엔진(parse_any_quotation과 동일한
draw_data 구조)으로 변환한다.

사용법 (auto_s3.py 쪽):
    from manual_form import manual_entry_form
    mode = st.radio("데이터 입력 방식", ["엑셀 업로드", "수기 실측 입력"], horizontal=True)
    if mode == "수기 실측 입력":
        draw_data, tongba_bom, unused_tongbas, max_dims, partner, site = manual_entry_form()
    else:
        # 기존 엑셀 업로드 로직 그대로
        ...
"""

import json
import base64
import re as _re
import pandas as pd
import streamlit as st

try:
    import requests
except ImportError:
    requests = None

# 사진 자동채움에 쓸 Gemini 모델 (무료 등급). 구글이 이름을 바꾸면 이 값만 교체.
GEMINI_MODEL = "gemini-2.5-flash"

# =====================================================================
# 1. 확정 드롭다운 목록 (견적서 49개에서 추출 — 수렴 완료)
# =====================================================================

# 제품명 → (정식 모델명, 자주 쓰는 창형태들)
PRODUCT_OPTIONS = {
    "발코니창141":     {"model": "HBF141S", "shapes": ["3W(1:2:1)", "2W", "2W(U/B)", "2W(1:2)", "3W(U/B)"]},
    "발코니이중창251": {"model": "HBF251D", "shapes": ["3W(1:2:1)", "2W", "2W(U/B)", "3W(U/B)"]},
    "일반이중창230":   {"model": "BF230R",  "shapes": ["3W(1:2:1)", "2W", "2W(U/B)"]},
    "일반단창115":     {"model": "BF115R",  "shapes": ["2W", "3W(1:2:1)"]},
    "공틀창225":       {"model": "BF225TM", "shapes": ["3W(1:2:1)", "2W", "2W(U/B)", "2W(1:2)"]},
    "고정창":          {"model": "고정창",  "shapes": ["CB-90+FM - 100면 유리", "CB-90+FM - 45면 유리", "CB-90(보강)+FM - 100면 유리"]},
    "터닝도어":        {"model": "HDF140",  "shapes": ["미는문"]},
}
PRODUCT_LIST = list(PRODUCT_OPTIONS.keys())

# 창형태 전체 (모든 제품의 형태 합집합 — 데이터에디터 셀렉트박스용)
SHAPE_LIST = ["3W(1:2:1)", "2W", "2W(U/B)", "2W(1:2)", "3W(U/B)", "미는문",
              "CB-90+FM - 100면 유리", "CB-90+FM - 45면 유리", "CB-90(보강)+FM - 100면 유리"]

# 유리 — 내부(P_ 접두어) / 외부(P_ 없음)
GLASS_INNER = ["P_28T 더블로이+투명(V)", "P_24T 미스트+로이(V)", "P_24T 투명+로이(V)",
               "P_24T 더블로이+투명(V)", "P_28T 더블로이+미스트(V)", "P_24T 아쿠아+로이(V)",
               "28T 투명+투명(V)", "24T 투명+투명(V)"]
GLASS_OUTER = ["(없음)", "24T 투명+투명(V)", "28T 투명+투명(V)", "24T 미스트+투명(V)",
               "28T 미스트+투명(V)", "24T 투명+로이(V)"]

VENT_DIRS   = ["N", "좌", "우"]
TONGBA_4SIDE = ["(없음)", "CB-100*45", "CB-101*100", "CB-45*45", "CB-135"]

# 수기 약어 → 정식 제품명 (사진/메모 자동 변환용 참고 매핑)
SHORTHAND_MAP = {
    "141단창": "발코니창141", "발코니141": "발코니창141",
    "251이중": "발코니이중창251", "발코니251": "발코니이중창251",
    "230이중": "일반이중창230", "115단창": "일반단창115",
    "225공틀": "공틀창225", "225공통": "공틀창225",
}


# =====================================================================
# 2. 폼 입력행 → draw_data 변환 (순수 함수 — streamlit 불필요, 테스트 가능)
# =====================================================================
def rows_to_drawdata(rows):
    """rows: dict의 리스트 (폼 한 줄 = 창 하나).
    반환: (windows_for_drawing, tongba_bom, unused_tongbas, (max_w, max_h), partner, site)
    parse_any_quotation과 동일한 구조."""
    windows = []
    tongba_bom = []
    seq = 0

    for r in rows:
        prod = str(r.get("제품", "")).strip()
        if prod not in PRODUCT_OPTIONS:
            continue  # 빈 행/잘못된 행 건너뜀
        try:
            w_val = int(float(r.get("가로W") or 0))
            h_val = int(float(r.get("세로H") or 0))
        except (ValueError, TypeError):
            continue
        if w_val <= 0 or h_val <= 0:
            continue  # 치수 없는 행 무시

        seq += 1
        model = PRODUCT_OPTIONS[prod]["model"]
        shape = str(r.get("형태", "")).strip() or PRODUCT_OPTIONS[prod]["shapes"][0]

        # 유리
        g_in = str(r.get("내부유리", "")).strip()
        g_out = str(r.get("외부유리", "")).strip()
        if g_out in ("(없음)", "", "nan"):
            g_out = ""

        # 벤트 사이즈 / 방향
        try:
            w1 = int(float(r.get("벤트W1") or 0))
        except (ValueError, TypeError):
            w1 = 0
        vent = str(r.get("방향", "N")).strip()
        vent_dir = "" if vent in ("N", "", "nan") else vent

        # 핸들높이
        try:
            hh = int(float(r.get("핸들높이") or 0))
            handle_height = hh if 100 <= hh <= 3000 else ""
        except (ValueError, TypeError):
            handle_height = ""

        has_screen = bool(r.get("방충망", False))

        # 사면통바 → 상/하(W길이) · 좌/우(H길이)
        auto_top = auto_bot = auto_left = auto_right = ""
        tongba = str(r.get("사면통바", "(없음)")).strip()
        if tongba and tongba not in ("(없음)", "", "nan"):
            auto_top = auto_bot = f"{tongba}({w_val})"
            auto_left = auto_right = f"{tongba}({h_val})"
            loc = str(r.get("위치", "")).strip()
            tongba_bom.append({"위치": loc, "자재명": tongba, "길이": w_val, "수량": 2})
            tongba_bom.append({"위치": loc, "자재명": tongba, "길이": h_val, "수량": 2})

        windows.append({
            "순번": seq,
            "위치": str(r.get("위치", "")).strip(),
            "제품명": prod,
            "모델명": model,
            "형태": shape,
            "glass_in": g_in,
            "glass_out": g_out,
            "가로(W)": w_val,
            "세로(H)": h_val,
            "w1": w1,
            "핸들높이": handle_height,
            "vent_dir": vent_dir,
            "has_screen": has_screen,
            "auto_top": auto_top, "auto_bot": auto_bot,
            "auto_left": auto_left, "auto_right": auto_right,
            "qty": 1, "repeat_count": 1, "unit_w": w_val,
        })

    max_w = max([win["가로(W)"] for win in windows] + [2500])
    max_h = max([win["세로(H)"] for win in windows] + [2500])
    return windows, tongba_bom, [], (max_w, max_h), "", ""


# =====================================================================
# 3. Streamlit 입력 폼 UI
# =====================================================================
def _blank_df():
    return pd.DataFrame([{
        "위치": "", "제품": PRODUCT_LIST[0], "형태": "3W(1:2:1)",
        "가로W": 0, "세로H": 0, "벤트W1": 0, "방향": "N",
        "내부유리": GLASS_INNER[0], "외부유리": "(없음)",
        "핸들높이": 0, "방충망": True, "사면통바": "(없음)",
    }])


def manual_entry_form():
    """수기 실측 입력 폼을 그리고, parse_any_quotation과 동일한 튜플을 반환."""
    st.subheader("✍️ 수기 실측 입력")
    st.caption("실측지를 보고 한 줄에 창 하나씩 입력하세요. 아래 표에서 행 추가/삭제가 가능합니다. "
               "제품을 고르면 모델명은 자동으로 연결됩니다.")

    if "manual_df" not in st.session_state:
        st.session_state.manual_df = _blank_df()

    # 📸 사진 자동채움 (선택) — 실측지 사진을 올리면 표가 자동으로 채워진다.
    _photo_autofill_section()

    edited = st.data_editor(
        st.session_state.manual_df,
        num_rows="dynamic",
        use_container_width=True,
        key="manual_editor",
        column_config={
            "위치": st.column_config.TextColumn("적용공간", width="medium", help="예: 거실발코니"),
            "제품": st.column_config.SelectboxColumn("제품", options=PRODUCT_LIST, required=True),
            "형태": st.column_config.SelectboxColumn("창형태", options=SHAPE_LIST, required=True),
            "가로W": st.column_config.NumberColumn("가로(W)", min_value=0, max_value=10000, step=10),
            "세로H": st.column_config.NumberColumn("세로(H)", min_value=0, max_value=10000, step=10),
            "벤트W1": st.column_config.NumberColumn("벤트W1", min_value=0, max_value=10000, step=10,
                                                  help="U/B 등 벤트 사이즈. 없으면 0"),
            "방향": st.column_config.SelectboxColumn("방향", options=VENT_DIRS, default="N"),
            "내부유리": st.column_config.SelectboxColumn("내부유리", options=GLASS_INNER, width="large"),
            "외부유리": st.column_config.SelectboxColumn("외부유리(이중창)", options=GLASS_OUTER, width="large"),
            "핸들높이": st.column_config.NumberColumn("핸들높이", min_value=0, max_value=3000, step=10,
                                                  help="없으면 0"),
            "방충망": st.column_config.CheckboxColumn("방충망", default=True),
            "사면통바": st.column_config.SelectboxColumn("사면통바", options=TONGBA_4SIDE, default="(없음)"),
        },
    )
    st.session_state.manual_df = edited

    rows = edited.to_dict("records")
    result = rows_to_drawdata(rows)
    n = len(result[0])
    if n == 0:
        st.info("가로·세로 치수가 입력된 창이 아직 없습니다. 표에 입력하면 도면이 생성됩니다.")
    else:
        st.success(f"✅ 창 {n}개 입력됨 → 아래에서 도면을 확인하세요.")
    return result


# =====================================================================
# 4. 📸 사진 자동채움 (Gemini 무료 등급으로 실측지 사진 → 표 자동입력)
# =====================================================================

def _build_extract_prompt():
    """Gemini에게 보낼 추출 지시문 — 짧게 핵심만."""
    return (
        "한국 KCC홈씨씨 창호 수기 실측지 사진입니다. '3. 실측 내역' 표의 모든 행을 읽어 JSON 배열로 출력하세요.\n"
        "반드시 한 줄 압축 JSON. 들여쓰기/줄바꿈 금지. 표 끝까지 전부 읽으세요.\n\n"
        "키: 위치(적용공간), 제품(141단창/251이중/230이중/115단창/225공틀/고정창/터닝도어), "
        "형태(3W/2W/2W(U/B)/F 등), 가로W(mm정수), 세로H(mm정수), 벤트W1(mm,없으면0), "
        "방향(N/좌/우), 내부유리(텍스트), 외부유리(이중창만,없으면빈값), "
        "핸들높이(mm,없으면0), 방충망(true/false), 사면통바(CB-100*45등,없으면빈값)\n\n"
        "규칙:\n"
        "- 사면공통/통바 행(W*H 있음)은 공틀창으로 별도 출력 + 위 창의 사면통바에도 값 넣기\n"
        "- 비고란 통바(L만)는 무시\n"
        "- 특이사항의 망=#(망)→방충망true, VENT 1100→벤트W1=1100\n\n"
        "예시: [{\"위치\":\"거실\",\"제품\":\"141단창\",\"형태\":\"3W\",\"가로W\":4635,\"세로H\":2330,"
        "\"벤트W1\":0,\"방향\":\"N\",\"내부유리\":\"더블로이+투명\",\"외부유리\":\"\","
        "\"핸들높이\":0,\"방충망\":true,\"사면통바\":\"\"}]"
    )


def _extract_json(text):
    """모델 응답에서 JSON 배열만 안전하게 추출. 잘린 JSON도 복구 시도."""
    text = (text or "").strip()
    text = _re.sub(r"^```(json)?|```$", "", text, flags=_re.MULTILINE).strip()

    # 1차: 그대로 파싱
    try:
        data = json.loads(text)
        if isinstance(data, dict): data = [data]
        return data if isinstance(data, list) else []
    except Exception:
        pass

    # 2차: [...]를 찾아 파싱
    m = _re.search(r"\[.*\]", text, flags=_re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict): data = [data]
            return data if isinstance(data, list) else []
        except Exception:
            pass

    # 3차: 잘린 JSON 복구 — [ 로 시작하지만 ] 없이 끊긴 경우
    start = text.find("[")
    if start >= 0:
        fragment = text[start:]
        # 마지막 완전한 },까지 자르고 ]를 붙여서 복구
        for trim in [
            fragment.rstrip(),                    # 그대로 + ]
            _re.sub(r',\s*\{[^}]*$', '', fragment),  # 불완전한 마지막 객체 제거
            _re.sub(r',\s*$', '', fragment),       # 끝의 쉼표 제거
        ]:
            for suffix in ["]", "}]", "\"}]"]:
                candidate = trim.rstrip().rstrip(",").rstrip() + suffix
                try:
                    data = json.loads(candidate)
                    if isinstance(data, dict): data = [data]
                    if isinstance(data, list) and len(data) > 0:
                        return data
                except Exception:
                    continue

    return []


def _gemini_extract(image_bytes, mime, api_key):
    if requests is None:
        raise RuntimeError("requests 패키지가 필요합니다. requirements.txt에 requests를 추가하세요.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [{"parts": [
            {"text": _build_extract_prompt()},
            {"inline_data": {"mime_type": mime or "image/jpeg",
                             "data": base64.b64encode(image_bytes).decode()}},
        ]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 8192},
    }
    r = requests.post(url, headers={"x-goog-api-key": api_key,
                                    "Content-Type": "application/json"},
                      json=payload, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini API 오류 {r.status_code}: {r.text[:500]}")
    data = r.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        block_reason = ""
        try:
            block_reason = str(data.get("promptFeedback", {}).get("blockReason", ""))
        except Exception:
            pass
        raise RuntimeError(f"응답 파싱 실패 (차단사유: {block_reason}): {str(data)[:500]}")
    # 디버그용: 모델 중단 이유
    finish_reason = ""
    try:
        finish_reason = data["candidates"][0].get("finishReason", "")
    except Exception:
        pass
    return text, finish_reason


# ---- 값 정규화: 모델이 살짝 다른 값을 줘도 드롭다운 옵션에 맞춰 깨지지 않게 ----
def _match_option(value, options, default):
    v = str(value or "").strip()
    if v in options:
        return v
    vc = v.replace(" ", "").lower()
    for o in options:                      # 공백/대소문자 무시 일치
        if o.replace(" ", "").lower() == vc:
            return o
    for o in options:                      # 부분 포함
        if vc and (vc in o.replace(" ", "").lower() or o.replace(" ", "").lower() in vc):
            return o
    return default


def _norm_product(v):
    v = str(v or "").strip()
    if v in PRODUCT_OPTIONS:
        return v
    if v in SHORTHAND_MAP:
        return SHORTHAND_MAP[v]
    vn = v.replace(" ", "")
    hints = [("251", "발코니이중창251"), ("141", "발코니창141"), ("230", "일반이중창230"),
             ("115", "일반단창115"), ("225", "공틀창225"), ("공틀", "공틀창225"), ("공통", "공틀창225"),
             ("고정", "고정창"), ("Fix", "고정창"), ("fix", "고정창"),
             ("터닝", "터닝도어"), ("미는", "터닝도어")]
    for key, prod in hints:
        if key in vn:
            return prod
    return PRODUCT_LIST[0]


def _to_int(v):
    try:
        return int(float(str(v).replace(",", "").strip()))
    except (ValueError, TypeError):
        return 0


def _norm_row(r):
    prod = _norm_product(r.get("제품"))
    shape = _match_option(r.get("형태"), SHAPE_LIST, PRODUCT_OPTIONS[prod]["shapes"][0])
    return {
        "위치": str(r.get("위치", "")).strip(),
        "제품": prod,
        "형태": shape,
        "가로W": _to_int(r.get("가로W")),
        "세로H": _to_int(r.get("세로H")),
        "벤트W1": _to_int(r.get("벤트W1")),
        "방향": _match_option(r.get("방향"), VENT_DIRS, "N"),
        "내부유리": _match_option(r.get("내부유리"), GLASS_INNER, GLASS_INNER[0]),
        "외부유리": _match_option(r.get("외부유리"), GLASS_OUTER, "(없음)"),
        "핸들높이": _to_int(r.get("핸들높이")),
        "방충망": bool(r.get("방충망", False)) if not isinstance(r.get("방충망"), str)
                  else str(r.get("방충망")).strip().lower() in ("true", "1", "y", "예", "o", "○", "망", "유", "ㅇ"),
        "사면통바": _match_option(r.get("사면통바"), TONGBA_4SIDE, "(없음)"),
    }


def _photo_autofill_section():
    with st.expander("📸 사진으로 자동 채우기 (선택) — 실측지 사진 → 표 자동입력", expanded=False):
        st.caption("실측지 사진을 올리고 버튼을 누르면 AI가 표를 채워줍니다. "
                   "⚠️ 개인정보 보호를 위해 가능하면 '실측 내역 표' 부분만 잘라서 올리세요. "
                   "(고객 이름·전화·주소가 담긴 상단은 제외 권장)")
        img = st.file_uploader("실측지 사진", type=["png", "jpg", "jpeg"], key="manual_photo")
        if img is None:
            return
        st.image(img, caption="업로드된 실측지", width=320)

        if st.button("🔍 사진에서 표 자동 채우기", type="primary", key="manual_autofill_btn"):
            # API 키 확인
            api_key = None
            try:
                api_key = st.secrets.get("GEMINI_API_KEY", None)
            except Exception:
                api_key = None
            if not api_key:
                st.error("GEMINI_API_KEY가 설정되어 있지 않습니다. "
                         "Streamlit 앱 설정(secrets)에 GEMINI_API_KEY를 추가하세요. "
                         "키 발급(무료): https://aistudio.google.com/apikey")
                return
            with st.spinner("AI가 실측지를 읽는 중..."):
                try:
                    raw_text, finish_reason = _gemini_extract(img.getvalue(), img.type, api_key)
                except Exception as e:
                    st.error(f"읽기 실패: {e}")
                    st.caption("💡 사진이 너무 어둡거나 흐리면 실패할 수 있습니다. 밝은 곳에서 다시 찍어보세요.")
                    return
            # 디버그: AI 응답 원문 표시 (접기)
            with st.expander("🔍 AI 응답 원문 보기 (디버그)", expanded=False):
                st.caption(f"중단사유: {finish_reason or '없음'} | 응답길이: {len(raw_text)}자")
                st.code(raw_text[:3000] if raw_text else "(빈 응답)", language="json")
            raw = _extract_json(raw_text)
            rows = [_norm_row(r) for r in raw if isinstance(r, dict)]
            rows = [r for r in rows if r["가로W"] > 0 or r["세로H"] > 0 or r["위치"]]
            if not rows:
                st.warning("표에서 창을 찾지 못했습니다. 사진이 선명한지 확인하거나 직접 입력해 주세요.")
                return
            st.session_state.manual_df = pd.DataFrame(rows)
            st.session_state.pop("manual_editor", None)  # 에디터 새로 그리게
            st.success(f"✅ {len(rows)}개 창을 읽었습니다! 아래 표에서 확인·수정하세요.")
            st.rerun()
