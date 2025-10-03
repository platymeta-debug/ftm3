# analysis/strategies/comprehensive_strategy.py (추천 지표 5종 추가 최종본)

import pandas as pd
from .base_strategy import BaseStrategy

class ComprehensiveStrategy(BaseStrategy):
    name = "종합 지표 전략"

    def __init__(self, params: dict):
        self.p = params # 설정값을 self.p에 저장

    def analyze(self, data: pd.DataFrame) -> dict:
        scores = {}
        last = data.iloc[-1]
        prev = data.iloc[-2] # 이전 캔들 데이터

        # === 추세 확인 지표 (Trend Confirmation) ===
        # (MACD, ADX, 일목균형표, PSAR, Vortex 로직은 이전과 동일)
        macd_col, macds_col = "MACD_12_26_9", "MACDs_12_26_9"
        if pd.notna(last.get(macd_col)) and pd.notna(last.get(macds_col)):
            if last[macd_col] > last[macds_col]: scores["MACD_Cross"] = self.p.get("score_macd_cross_up", 2)
            else: scores["MACD_Cross"] = self.p.get("score_macd_cross_down", -2)

        adx_col = "ADX_14"
        if pd.notna(last.get(adx_col)) and last[adx_col] > self.p.get("adx_threshold", 25):
            scores["ADX_Strength"] = self.p.get("score_adx_strong", 3)

        isa_col, isb_col = "ISA_9", "ISB_26"
        if pd.notna(last.get(isa_col)) and pd.notna(last.get(isb_col)):
            if last['close'] > last[isa_col] and last['close'] > last[isb_col]: scores["Ichimoku_Cloud"] = self.p.get("score_ichimoku_bull", 4)
            elif last['close'] < last[isa_col] and last['close'] < last[isb_col]: scores["Ichimoku_Cloud"] = self.p.get("score_ichimoku_bear", -4)

        psar_up_col, psar_down_col = "PSARl_0.02_0.2", "PSARs_0.02_0.2"
        if pd.notna(last.get(psar_up_col)): scores["PSAR"] = self.p.get("score_psar_bull", 3)
        elif pd.notna(last.get(psar_down_col)): scores["PSAR"] = self.p.get("score_psar_bear", -3)

        vip_col, vim_col = "VTXP_14", "VTXM_14"
        if pd.notna(last.get(vip_col)) and pd.notna(last.get(vim_col)):
            if last[vip_col] > last[vim_col]: scores["Vortex"] = self.p.get("score_vortex_bull", 2)
            else: scores["Vortex"] = self.p.get("score_vortex_bear", -2)

        # ▼▼▼ [신규] TRIX (추세 필터링) ▼▼▼
        trix_col, trixs_col = "TRIX_30_9", "TRIXs_30_9"
        if pd.notna(last.get(trix_col)) and pd.notna(last.get(trixs_col)):
            if last[trix_col] > last[trixs_col] and prev[trix_col] < prev[trixs_col]: # 골든크로스
                scores["TRIX"] = self.p.get("score_trix_cross_up", 4)
            elif last[trix_col] < last[trixs_col] and prev[trix_col] > prev[trixs_col]: # 데드크로스
                scores["TRIX"] = self.p.get("score_trix_cross_down", -4)
        
        # === 과매수/과매도 및 변동성 지표 ===
        # (볼린저밴드, CCI 로직은 이전과 동일)
        bbl_col, bbu_col, bbb_col = "BBL_20_2.0", "BBU_20_2.0", "BBB_20_2.0"
        if all(pd.notna(last.get(c)) for c in [bbl_col, bbu_col, bbb_col]):
            if last['close'] > last[bbu_col]: scores["BB_Breakout"] = self.p.get("score_bb_breakout_up", 4)
            elif last['close'] < last[bbl_col]: scores["BB_Breakout"] = self.p.get("score_bb_breakout_down", -4)
            if last[bbb_col] < data[bbb_col].rolling(90).quantile(0.1).iloc[-1]: scores["BB_Squeeze"] = self.p.get("score_bb_squeeze", 3)

        cci_col = f"CCI_{self.p.get('cci_length', 20)}_{self.p.get('cci_constant', 0.015)}"
        if pd.notna(last.get(cci_col)):
            if last[cci_col] > self.p.get("cci_overbought", 100): scores["CCI"] = self.p.get("score_cci_overbought", -3)
            elif last[cci_col] < self.p.get("cci_oversold", -100): scores["CCI"] = self.p.get("score_cci_oversold", 3)
        
        # ▼▼▼ [신규] 스토캐스틱 RSI (민감한 과매수/과매도) ▼▼▼
        stochrsi_k_col, stochrsi_d_col = "STOCHRSIk_14_14_3_3", "STOCHRSId_14_14_3_3"
        if pd.notna(last.get(stochrsi_d_col)):
            if last[stochrsi_d_col] < self.p.get("stochrsi_oversold", 20):
                scores["StochRSI"] = self.p.get("score_stochrsi_oversold", 3)
            elif last[stochrsi_d_col] > self.p.get("stochrsi_overbought", 80):
                scores["StochRSI"] = self.p.get("score_stochrsi_overbought", -3)
        
        # ▼▼▼ [신규] 켈트너 채널 (추세 돌파) ▼▼▼
        kcl_col, kcu_col = "KCL_20_2", "KCU_20_2"
        if pd.notna(last.get(kcu_col)) and pd.notna(last.get(kcl_col)):
            if last['close'] > last[kcu_col]: # 상단 채널 돌파
                scores["KC_Breakout"] = self.p.get("score_kc_breakout_up", 4)
            elif last['close'] < last[kcl_col]: # 하단 채널 돌파
                scores["KC_Breakout"] = self.p.get("score_kc_breakout_down", -4)

        # === 거래량 기반 지표 ===
        # (CMF 로직은 이전과 동일)
        cmf_col = "CMF_20"
        if pd.notna(last.get(cmf_col)) and last[cmf_col] > 0: scores["CMF"] = self.p.get("score_cmf_positive", 2)
        elif pd.notna(last.get(cmf_col)) and last[cmf_col] < 0: scores["CMF"] = self.p.get("score_cmf_negative", -2)
        
        # ▼▼▼ [신규] 엘더의 힘 지수 (추세의 힘) ▼▼▼
        efi_col = "EFI_13"
        if pd.notna(last.get(efi_col)):
            if last[efi_col] > 0 and prev[efi_col] < 0: # 0선 상향 돌파
                scores["EFI"] = self.p.get("score_efi_cross_up", 3)
            elif last[efi_col] < 0 and prev[efi_col] > 0: # 0선 하향 돌파
                scores["EFI"] = self.p.get("score_efi_cross_down", -3)
        
        # ▼▼▼ [신규] PPO (MACD의 표준화 버전) ▼▼▼
        ppo_col, ppos_col = "PPO_12_26_9", "PPOs_12_26_9"
        if pd.notna(last.get(ppo_col)) and pd.notna(last.get(ppos_col)):
            if last[ppo_col] > last[ppos_col]:
                scores["PPO"] = self.p.get("score_ppo_bull", 2)
            else:
                scores["PPO"] = self.p.get("score_ppo_bear", -2)

        # (CHOP 로직은 이전과 동일)
        chop_col = "CHOP_14_1_100"
        if pd.notna(last.get(chop_col)):
            if last[chop_col] < self.p.get("chop_trending_th", 40): scores["CHOP_Trend"] = self.p.get("score_chop_trending", 3)
            elif last[chop_col] > self.p.get("chop_sideways_th", 60): scores["CHOP_Trend"] = self.p.get("score_chop_sideways", -3)

        return scores