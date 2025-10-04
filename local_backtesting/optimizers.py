# -*- coding: utf-8 -*-
# local_backtesting/optimizers.py
import os
import random

try:
    from deap import base, creator, tools
    _HAS_DEAP = True
except Exception:
    _HAS_DEAP = False

try:
    from skopt import gp_minimize
    from skopt.space import Real, Integer, Categorical
    _HAS_SKOPT = True
except Exception:
    _HAS_SKOPT = False


def run_ga(objective_fn, param_spaces, seed=42):
    """
    GA 최적화.
    param_spaces 예:
    {
      "ema_short": {"type":"int","low":15,"high":25},
      "risk_reward_ratio":{"type":"float","low":2.0,"high":3.0},
      "score_macd_cross_up":{"type":"int","low":2,"high":4},
      "adx_threshold":{"type":"int","low":18,"high":26},
      "score_adx_strong":{"type":"int","low":2,"high":3},
      ...
    }
    objective_fn(dict) -> float (클수록 좋음)
    """
    if not _HAS_DEAP:
        raise RuntimeError("GA 사용을 위해 `pip install deap` 필요")

    random.seed(seed)
    pop_size = int(os.getenv("GA_POP_SIZE", 40))
    n_gen = int(os.getenv("GA_N_GENERATIONS", 18))

    # DEAP 준비
    creator.create("FitnessMax", base.Fitness, weights=(1.0,))
    creator.create("Individual", dict, fitness=creator.FitnessMax)
    toolbox = base.Toolbox()

    def init_individual():
        d = {}
        for k, s in param_spaces.items():
            t = s["type"]
            if t == "int":
                d[k] = random.randint(s["low"], s["high"])
            elif t == "float":
                d[k] = random.uniform(s["low"], s["high"])
            elif t == "cat":
                d[k] = random.choice(s["choices"])
        return creator.Individual(d)

    toolbox.register("individual", init_individual)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)

    def eval_ind(ind):
        # 간단한 제약 예시: ema_short < ema_long
        if "ema_short" in ind and "ema_long" in ind and ind["ema_short"] >= ind["ema_long"]:
            return (-1e12,)
        return (float(objective_fn(dict(ind))),)

    toolbox.register("evaluate", eval_ind)
    toolbox.register("mate", tools.cxTwoPoint)
    # 정수/연속 혼합 대비 변이: 개별 키를 다시 샘플
    def mutate_ind(ind, indpb=0.2):
        for k, s in param_spaces.items():
            if random.random() < indpb:
                t = s["type"]
                if t == "int":
                    ind[k] = random.randint(s["low"], s["high"])
                elif t == "float":
                    ind[k] = random.uniform(s["low"], s["high"])
                elif t == "cat":
                    ind[k] = random.choice(s["choices"])
        return (ind,)
    toolbox.register("mutate", mutate_ind, indpb=0.25)
    toolbox.register("select", tools.selTournament, tournsize=3)

    pop = toolbox.population(n=pop_size)
    CXPB, MUTPB = 0.6, 0.35

    best = None
    for _ in range(n_gen):
        offspring = tools.selTournament(pop, len(pop), tournsize=3)
        offspring = [creator.Individual(dict(x)) for x in offspring]

        # 교차
        for c1, c2 in zip(offspring[::2], offspring[1::2]):
            if random.random() < CXPB:
                toolbox.mate(c1, c2)
                if "fitness" in c1.__dict__: del c1.fitness.values
                if "fitness" in c2.__dict__: del c2.fitness.values

        # 변이
        for mut in offspring:
            if random.random() < MUTPB:
                toolbox.mutate(mut)
                if "fitness" in mut.__dict__: del mut.fitness.values

        invalid = [ind for ind in offspring if not ind.fitness.valid]
        fits = list(map(toolbox.evaluate, invalid))
        for ind, fit in zip(invalid, fits):
            ind.fitness.values = fit

        pop[:] = offspring
        gen_best = tools.selBest(pop, 1)[0]
        if best is None or gen_best.fitness.values[0] > best.fitness.values[0]:
            best = creator.Individual(dict(gen_best))
            best.fitness.values = gen_best.fitness.values

    return dict(best), float(best.fitness.values[0])


def run_bayes(objective_fn, param_spaces, seed=42):
    """
    베이지안 최적화 (skopt.gp_minimize).
    param_spaces 포맷은 run_ga와 동일.
    """
    if not _HAS_SKOPT:
        raise RuntimeError("Bayesian 사용을 위해 `pip install scikit-optimize` 필요")

    space, keys = [], []
    for k, s in param_spaces.items():
        keys.append(k)
        t = s["type"]
        if t == "int":
            space.append(Integer(s["low"], s["high"], name=k))
        elif t == "float":
            space.append(Real(s["low"], s["high"], name=k))
        elif t == "cat":
            space.append(Categorical(s["choices"], name=k))

    n_calls = int(os.getenv("BAYES_N_CALLS", 60))
    n_random_starts = int(os.getenv("BAYES_N_RANDOM_STARTS", 12))

    # gp_minimize는 최소화 → 부호 반전
    def _min_obj(x):
        params = {k: v for k, v in zip(keys, x)}
        if "ema_short" in params and "ema_long" in params and params["ema_short"] >= params["ema_long"]:
            return 1e12
        return -float(objective_fn(params))

    res = gp_minimize(_min_obj, space,
                      n_calls=n_calls,
                      n_random_starts=n_random_starts,
                      random_state=seed,
                      acq_func="EI")
    best_params = {k: v for k, v in zip(keys, res.x)}
    best_score = -res.fun
    return best_params, float(best_score)
