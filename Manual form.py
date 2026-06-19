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

import pandas as pd
import streamlit as st

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
