# local_backtesting/optimizers.py
# v3.0 — Flexible param space (choices/range) + GA & Bayesian (with skopt fallback)

from __future__ import annotations

import math
import random
import time
from typing import Any, Dict, List, Tuple, Callable, Optional

# ---- (옵션) skopt가 있으면 베이지안 최적화 사용, 없으면 랜덤 탐색 폴백 ----
try:
    from skopt import gp_minimize
    from skopt.space import Integer, Real, Categorical
    _HAS_SKOPT = True
except Exception:
    _HAS_SKOPT = False
    Integer = Real = Categorical = object  # type: ignore


# =============================================================================
# 공통: 파라미터 스페이스 유틸
# 입력 포맷 예시:
#   {
#     "open_threshold": {"type":"int", "choices":[10,12,14,16]}  # or low/high
#     "rr": {"type":"float", "low":1.5, "high":3.0}
#     "exec_trailing_mode":{"type":"cat","choices":["off","atr","percent"]}
#   }
# =============================================================================

def _space_item_to_sampler(name: str, spec: Dict[str, Any]) -> Tuple[Callable[[], Any], Callable[[Any], Any], Dict]:
    """
    각 파라미터에 대해 (sampler, mutator, meta) 반환
    - sampler(): 무작위 표본 생성
    - mutator(val): 값의 '조금' 변형(돌연변이)
    - meta: {"type":..., "domain":..., "choices":...}
    """
    ptype = (spec.get("type") or "").lower()
    choices = spec.get("choices")
    low = spec.get("low")
    high = spec.get("high")

    # 정리: 범위형이 없고 choices만 있으면 범위 대체
    if ptype in ("int", "float") and (low is None or high is None):
        if choices:
            low, high = (min(choices), max(choices))
        else:
            # 안전한 기본 범위
            low, high = (0, 1) if ptype == "int" else (0.0, 1.0)

    if ptype == "int":
        if choices and len(choices) > 0:
            pool = sorted(set(int(x) for x in choices))
            def sampler():
                return random.choice(pool)
            def mutator(v):
                if len(pool) == 1:
                    return pool[0]
                # 이웃 값으로 이동
                i = pool.index(int(v)) if v in pool else random.randrange(len(pool))
                j = max(0, min(len(pool)-1, i + random.choice([-1, 1])))
                return pool[j]
            return sampler, mutator, {"type": "int", "choices": pool}
        else:
            lo, hi = int(low), int(high)
            def sampler():
                return random.randint(lo, hi)
            def mutator(v):
                if lo == hi:
                    return lo
                step = max(1, (hi - lo) // 20)
                return max(lo, min(hi, int(v) + random.randint(-step, step)))
            return sampler, mutator, {"type": "int", "low": lo, "high": hi}

    if ptype == "float":
        if choices and len(choices) > 0:
            pool = sorted(set(float(x) for x in choices))
            def sampler():
                return random.choice(pool)
            def mutator(v):
                if len(pool) == 1:
                    return pool[0.0]
                i = pool.index(float(v)) if v in pool else random.randrange(len(pool))
                j = max(0, min(len(pool)-1, i + random.choice([-1, 1])))
                return pool[j]
            return sampler, mutator, {"type": "float", "choices": pool}
        else:
            lo, hi = float(low), float(high)
            def sampler():
                return random.uniform(lo, hi)
            def mutator(v):
                width = (hi - lo)
                jitter = width * 0.05  # 5% 범위
                nv = float(v) + random.uniform(-jitter, jitter)
                return max(lo, min(hi, nv))
            return sampler, mutator, {"type": "float", "low": lo, "high": hi}

    # 카테고리
    pool = (choices or spec.get("categories") or [])
    pool = list(pool)
    if not pool:
        pool = [None]
    def sampler():
        return random.choice(pool)
    def mutator(v):
        if len(pool) == 1:
            return pool[0]
        x = random.choice(pool)
        return x if x != v else random.choice(pool)
    return sampler, mutator, {"type": "cat", "categories": pool}


def _build_samplers(param_spaces: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    samplers = {}
    for k, spec in param_spaces.items():
        sp, mu, meta = _space_item_to_sampler(k, spec)
        samplers[k] = {"sample": sp, "mutate": mu, "meta": meta}
    return samplers


def _sample_params(samplers: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return {k: v["sample"]() for k, v in samplers.items()}


def _mutate_params(samplers: Dict[str, Dict[str, Any]], params: Dict[str, Any], prob: float = 0.2) -> Dict[str, Any]:
    out = dict(params)
    for k, s in samplers.items():
        if random.random() < prob:
            out[k] = s["mutate"](out[k])
    return out


# =============================================================================
# 베이지안 최적화 (skopt 사용, 없으면 랜덤 탐색)
# =============================================================================

def _to_skopt_space(param_spaces: Dict[str, Dict[str, Any]]) -> Tuple[List[Any], List[str]]:
    """
    skopt용 space, key 순서 반환. (low/high 또는 choices/categories 모두 지원)
    - low==high인 경우 Categorical([값])로 다운그레이드하여 skopt 오류 방지
    """
    if not _HAS_SKOPT:
        return [], []
    space, keys = [], []
    for k, s in param_spaces.items():
        t = (s.get("type") or "").lower()
        ch = s.get("choices") or s.get("categories")
        lo, hi = s.get("low"), s.get("high")

        if t in ("int", "float"):
            # 범위가 없으면 choices로부터 유도
            if lo is None or hi is None:
                if ch:
                    lo, hi = (min(ch), max(ch))
                else:
                    lo, hi = (0, 1) if t == "int" else (0.0, 1.0)
            # 단일값 범위는 Categorical로 대체
            if float(lo) == float(hi):
                cats = [int(lo)] if t == "int" else [float(lo)]
                space.append(Categorical(cats, name=k))
                keys.append(k)
                continue
            if t == "int":
                space.append(Integer(int(lo), int(hi), name=k))
            else:
                space.append(Real(float(lo), float(hi), name=k))
            keys.append(k)
        else:
            cats = list(ch) if ch else [None]
            space.append(Categorical(cats, name=k))
            keys.append(k)
    return space, keys


def run_bayes(objective: Callable[[Dict[str, Any]], float],
              param_spaces: Dict[str, Dict[str, Any]],
              n_calls: int = 60,
              n_random_starts: int = 12,
              random_state: int = 42) -> Tuple[Dict[str, Any], float]:
    """
    objective(params) -> score (큰 값이 좋음)
    반환: (best_params_dict, best_score)
    """
    random.seed(random_state)

    # skopt가 있으면 gp_minimize 사용 (minimize → 부호 반전)
    if _HAS_SKOPT:
        space, keys = _to_skopt_space(param_spaces)
        if space and keys:
            def _vec2dict(vec):
                return {k: v for k, v in zip(keys, vec)}
            def _wrapped(vec):
                params = _vec2dict(vec)
                score = objective(params)  # 높은 게 좋음
                if score is None or math.isnan(score):
                    return 1e12
                return -float(score)  # minimize
            res = gp_minimize(
                _wrapped,
                space,
                n_calls=n_calls,
                n_random_starts=n_random_starts,
                random_state=random_state,
                noise=1e-10,
            )
            best_params = {k: v for k, v in zip(keys, res.x)}
            best_score = -float(res.fun)
            return best_params, best_score

    # 폴백: 랜덤 탐색
    samplers = _build_samplers(param_spaces)
    best_p, best_s = None, -1e18
    for _ in range(n_calls):
        p = _sample_params(samplers)
        s = objective(p)
        if s is not None and s > best_s:
            best_p, best_s = dict(p), float(s)
    return best_p or {}, float(best_s)


# =============================================================================
# 유전 알고리즘 (간단 구현, 외부 라이브러리 불필요)
# =============================================================================

def run_ga(objective: Callable[[Dict[str, Any]], float],
           param_spaces: Dict[str, Dict[str, Any]],
           pop_size: int = 32,
           generations: int = 40,
           elite_frac: float = 0.20,
           cx_prob: float = 0.8,
           mut_prob: float = 0.2,
           random_state: int = 42) -> Tuple[Dict[str, Any], float]:
    """
    간단 GA:
      - 초기 무작위 모집단
      - 상위 elite 보존 + 토너먼트 선택
      - 1점 교차 + 돌연변이
      - 목적함수는 '큰 값이 좋음'
    """
    random.seed(random_state)
    samplers = _build_samplers(param_spaces)

    def _evaluate(pop: List[Dict[str, Any]]) -> List[Tuple[float, Dict[str, Any]]]:
        scored = []
        for ind in pop:
            try:
                val = float(objective(ind))
            except Exception:
                val = -1e18
            scored.append((val, ind))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    def _tournament(pop: List[Dict[str, Any]], k: int = 3) -> Dict[str, Any]:
        cand = random.sample(pop, k=min(k, len(pop)))
        best = None
        best_s = -1e18
        for c in cand:
            s = float(objective(c))
            if s > best_s:
                best, best_s = c, s
        return dict(best)

    def _crossover(p1: Dict[str, Any], p2: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if random.random() > cx_prob or not p1 or not p2:
            return dict(p1), dict(p2)
        keys = list(p1.keys())
        cut = random.randrange(1, len(keys)) if len(keys) > 1 else 1
        k_left = keys[:cut]
        k_right = keys[cut:]
        c1 = {**{k: p1[k] for k in k_left}, **{k: p2[k] for k in k_right}}
        c2 = {**{k: p2[k] for k in k_left}, **{k: p1[k] for k in k_right}}
        return c1, c2

    # 초기 모집단
    population: List[Dict[str, Any]] = [_sample_params(samplers) for _ in range(pop_size)]
    hall_best = None
    hall_score = -1e18

    for gen in range(generations):
        scored = _evaluate(population)
        elite_n = max(1, int(pop_size * elite_frac))
        elites = [dict(x[1]) for x in scored[:elite_n]]

        # Hall of Fame
        if scored[0][0] > hall_score:
            hall_score = scored[0][0]
            hall_best = dict(scored[0][1])

        # 새 세대 생성
        new_pop: List[Dict[str, Any]] = list(elites)
        while len(new_pop) < pop_size:
            p1 = _tournament(population, k=3)
            p2 = _tournament(population, k=3)
            c1, c2 = _crossover(p1, p2)
            c1 = _mutate_params(samplers, c1, prob=mut_prob)
            c2 = _mutate_params(samplers, c2, prob=mut_prob)
            new_pop.append(c1)
            if len(new_pop) < pop_size:
                new_pop.append(c2)

        population = new_pop

    # 마지막 평가
    final_scored = _evaluate(population)
    top_score, top_params = final_scored[0]
    if hall_best is not None and hall_score >= top_score:
        return hall_best, float(hall_score)
    return dict(top_params), float(top_score)
