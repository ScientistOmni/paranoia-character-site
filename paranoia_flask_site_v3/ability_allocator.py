from __future__ import annotations

"""
キャラクター能力値決定プログラム
Python 3.10+
依存: numpy, scipy

インストール:
    python -m pip install numpy scipy

実行:
    python ability_allocator.py
"""

from dataclasses import dataclass, field
from typing import List, Optional
import random
import os
import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

X = int(os.environ.get("PLAYER_COUNT", "7"))
if not 1 <= X <= 10:
    raise ValueError("PLAYER_COUNT は1〜10の整数にしてください")
M = 16
N_STEPS = 5
U = {1: 20, 2: 10, 3: 5}
C = 5
INITIAL_XP = 100

# 第1希望の評価値に加えるタイブレーク項の最大値。
# 評価値の本体は0.2刻みなので、0.001以下なら通常の評価差を逆転させず、
# 本体が同値の場合に入力順を反映できる。
V_MAX = 0.001


@dataclass
class PlayerInput:
    # 能力番号は内部では0..15。表示上はA1..A16。
    preferences: List[int]  # 第1〜第3希望。1〜3個
    bid: int = 0             # 優先権として支払うXP
    submitted_order: int = 1 # そのステップでの入力順。1が最も早い


@dataclass
class PlayerState:
    values: List[int] = field(default_factory=lambda: [0] * M)
    # 値が後に0に戻った場合も含め、一度でも変動した能力は再利用禁止。
    used: List[bool] = field(default_factory=lambda: [False] * M)
    xp: int = INITIAL_XP
    total_bid_paid: int = 0
    total_bid_refunded: int = 0


@dataclass
class Move:
    player: int
    ability: int
    score: float
    rank: Optional[int]
    neighbor_rank: Optional[int]
    v_bonus: float
    net_xp_cost: int


def rank_of(preferences: List[int], ability: int) -> Optional[int]:
    try:
        return preferences.index(ability) + 1
    except ValueError:
        return None


def validate_inputs(inputs: List[PlayerInput], states: List[PlayerState]) -> None:
    if len(inputs) != X or len(states) != X:
        raise ValueError(f"プレイヤー数は{X}人である必要があります")

    orders = [p.submitted_order for p in inputs]
    if sorted(orders) != list(range(1, X + 1)):
        raise ValueError(f"submitted_order は1〜{X}の重複しない整数にしてください")

    for i, item in enumerate(inputs):
        if not 1 <= len(item.preferences) <= 3:
            raise ValueError(f"P{i+1}: 希望は1〜3個指定してください")
        if len(set(item.preferences)) != len(item.preferences):
            raise ValueError(f"P{i+1}: 希望能力が重複しています")
        if any(not isinstance(j, int) or not 0 <= j < M for j in item.preferences):
            raise ValueError(f"P{i+1}: 能力番号は0〜{M-1}で指定してください")
        if not isinstance(item.bid, int) or item.bid < 0:
            raise ValueError(f"P{i+1}: 優先権額は0以上の整数にしてください")
        if item.bid > states[i].xp:
            raise ValueError(f"P{i+1}: 優先権額が残りXPを超えています")


def v_bonus_for(submitted_order: int) -> float:
    """早い入力ほど大きいV。参加人数Xに応じて1番目が最大になる。"""
    return V_MAX * (X + 1 - submitted_order) / X


def solve_step(step_n: int, inputs: List[PlayerInput],
               states: List[PlayerState]) -> List[Move]:
    if step_n not in range(1, N_STEPS + 1):
        raise ValueError("step_n は1〜5です")
    validate_inputs(inputs, states)

    # 各候補 x_(i,j): PiがAjを+nし、P(i+1)がAjを-nする。
    candidates = []
    for i in range(X):
        nxt = (i + 1) % X
        for j in range(M):
            if states[i].used[j] or states[nxt].used[j]:
                continue
            if states[i].values[j] != 0 or states[nxt].values[j] != 0:
                continue

            r_i = rank_of(inputs[i].preferences, j)
            r_next = rank_of(inputs[nxt].preferences, j)

            # 希望に含まれない能力は評価値0として候補に残す。
            # a_ij
            if r_i == 1:
                a_score = U[1] + inputs[i].bid + v_bonus_for(inputs[i].submitted_order)
            elif r_i == 2:
                a_score = U[2]
            elif r_i == 3:
                a_score = U[3]
            else:
                a_score = 0.0

            # b_(i+1),j。希望に含まれない場合は0。
            if r_next == 1:
                b_score = -(U[1] + inputs[nxt].bid) / C
            elif r_next == 2:
                b_score = -U[2] / C
            elif r_next == 3:
                b_score = -U[3] / C
            else:
                b_score = 0.0

            candidates.append({
                "player": i,
                "ability": j,
                "score": a_score + b_score,
                "rank": r_i,
                "neighbor_rank": r_next,
                "v_bonus": v_bonus_for(inputs[i].submitted_order) if r_i == 1 else 0.0,
            })

    if not candidates:
        raise RuntimeError(f"Step {step_n}: 利用可能な候補がありません")

    # 二値整数最適化:
    # ・各プレイヤーは各ステップでちょうど1能力を上昇させる
    # ・同一能力を同じステップに2人以上が上昇させない
    k_count = len(candidates)
    A = lil_matrix((X + M, k_count), dtype=float)
    for k, cand in enumerate(candidates):
        A[cand["player"], k] = 1
        A[X + cand["ability"], k] = 1

    lower = np.zeros(X + M)
    upper = np.ones(X + M)
    lower[:X] = 1
    constraints = LinearConstraint(A.tocsr(), lower, upper)

    result = milp(
        c=-np.array([cand["score"] for cand in candidates], dtype=float),
        integrality=np.ones(k_count),
        bounds=Bounds(np.zeros(k_count), np.ones(k_count)),
        constraints=constraints,
        options={"time_limit": 15.0},
    )
    if not result.success or result.x is None:
        raise RuntimeError(f"Step {step_n}: 最適化失敗: {result.message}")

    chosen = [candidates[k] for k, x in enumerate(result.x) if x > 0.5]
    if len(chosen) != X or len({c["player"] for c in chosen}) != X:
        raise RuntimeError(f"Step {step_n}: 全員に1件ずつ割り当てられませんでした")

    chosen_ability = {c["player"]: c["ability"] for c in chosen}

    # XP処理:
    # Step開始時に入札額を一時的に差し引き、ステップの割当確定後に返却する。
    # 「第1希望を獲得できなかった」OR「同じ能力を第1希望にした他人がいない」
    # 場合は返却。よって、獲得かつ競合時だけ最終的に消費される。
    net_cost = [0] * X
    for i, item in enumerate(inputs):
        bid = item.bid
        if bid == 0:
            continue

        states[i].xp -= bid
        states[i].total_bid_paid += bid

        first_choice = item.preferences[0]
        got_first = chosen_ability[i] == first_choice
        another_player_also_wants_it_first = any(
            k != i and other.preferences[0] == first_choice
            for k, other in enumerate(inputs)
        )
        refund = 0 if (got_first and another_player_also_wants_it_first) else bid

        # 返却は最適化・割当決定の後、ステップの最後に行う。
        states[i].xp += refund
        states[i].total_bid_refunded += refund
        net_cost[i] = bid - refund

    moves = []
    for cand in chosen:
        i, j = cand["player"], cand["ability"]
        nxt = (i + 1) % X

        # 防御的な二重チェック
        if states[i].used[j] or states[nxt].used[j]:
            raise AssertionError("変更済み能力を再利用しようとしました")
        if states[i].values[j] != 0 or states[nxt].values[j] != 0:
            raise AssertionError("非0の能力を変更しようとしました")

        states[i].values[j] = step_n
        states[nxt].values[j] = -step_n
        states[i].used[j] = True
        states[nxt].used[j] = True

        moves.append(Move(
            player=i, ability=j, score=cand["score"], rank=cand["rank"],
            neighbor_rank=cand["neighbor_rank"], v_bonus=cand["v_bonus"],
            net_xp_cost=net_cost[i],
        ))

    # 能力値の許容範囲と履歴フラグを確認
    allowed = {-5, -4, -3, -2, -1, 0, 1, 2, 3, 4, 5}
    for i, state in enumerate(states):
        if any(v not in allowed for v in state.values):
            raise AssertionError(f"P{i+1}: 能力値が許容範囲外です")
        for j, value in enumerate(state.values):
            if value != 0 and not state.used[j]:
                raise AssertionError(f"P{i+1}, A{j+1}: 履歴フラグに不整合があります")

    return sorted(moves, key=lambda m: m.player)


def validate_final(states: List[PlayerState]) -> None:
    for i, state in enumerate(states):
        positive = sorted(v for v in state.values if v > 0)
        negative = sorted(v for v in state.values if v < 0)
        zeros = sum(v == 0 for v in state.values)
        if positive != [1, 2, 3, 4, 5]:
            raise AssertionError(f"P{i+1}: 正の値が不正: {positive}")
        if negative != [-5, -4, -3, -2, -1]:
            raise AssertionError(f"P{i+1}: 負の値が不正: {negative}")
        if zeros != 6:
            raise AssertionError(f"P{i+1}: 0の個数が{zeros}（期待値6）")


def make_test_data(seed: int = 20261010) -> List[List[PlayerInput]]:
    """
    再現可能なテスト入力を生成する。
    submitted_order はプレイヤーIDと異なる順序にもなり得る。
    希望に含まれない能力も候補になるため、完全割当可能性は高いが、
    履歴制約により割当不能になるケースはsolve_stepが明示的に失敗する。
    """
    rng = random.Random(seed)
    steps = []
    for step in range(1, N_STEPS + 1):
        step_inputs = []
        orders = list(range(1, X + 1))
        rng.shuffle(orders)
        for i in range(X):
            prefs = rng.sample(range(M), 3)
            bid = rng.choice([0, 0, 1, 2, 3, 5])
            step_inputs.append(PlayerInput(
                preferences=prefs,
                bid=bid,
                submitted_order=orders[i],
            ))
        steps.append(step_inputs)
    return steps


def export_test_data(path: str = "test_data.json") -> None:
    data = make_test_data()
    obj = {"description": "能力番号はA1〜A16。submitted_order=1が最も早い入力。",
           "steps": []}
    for step_no, inputs in enumerate(data, start=1):
        obj["steps"].append({
            "step": step_no,
            "players": [
                {
                    "player_id": i + 1,
                    "preferences": [f"A{j+1}" for j in p.preferences],
                    "bid_xp": p.bid,
                    "submitted_order": p.submitted_order,
                }
                for i, p in enumerate(inputs)
            ]
        })
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def main() -> None:
    inputs_by_step = make_test_data()
    states = [PlayerState() for _ in range(X)]

    print("=== 入力データ ===")
    for n, inputs in enumerate(inputs_by_step, start=1):
        print(f"\nStep {n}")
        for i, p in enumerate(inputs, start=1):
            prefs = " > ".join(f"A{j+1}" for j in p.preferences)
            print(f"P{i}: {prefs}; bid={p.bid}; 入力順={p.submitted_order}")

    print("\n=== 最適化結果 ===")
    for n, inputs in enumerate(inputs_by_step, start=1):
        moves = solve_step(n, inputs, states)
        print(f"\nStep {n}（変動幅 {n}）")
        for move in moves:
            nxt = (move.player + 1) % X
            rank_text = f"第{move.rank}希望" if move.rank else "希望外（評価0）"
            neighbor_text = f"第{move.neighbor_rank}希望" if move.neighbor_rank else "希望外（評価0）"
            print(
                f"P{move.player+1}: A{move.ability+1} +{n} ({rank_text}) / "
                f"P{nxt+1}: A{move.ability+1} -{n} ({neighbor_text}); "
                f"評価={move.score:.4f}, V={move.v_bonus:.6f}, "
                f"正味XP消費={move.net_xp_cost}"
            )

    validate_final(states)
    print("\n=== 最終能力値 ===")
    for i, state in enumerate(states, start=1):
        print(f"P{i}: {state.values}")
        print(
            f"  残XP={state.xp}, 支払累計={state.total_bid_paid}, "
            f"返却累計={state.total_bid_refunded}"
        )
    print("\n最終制約チェック: OK")


if __name__ == "__main__":
    main()
