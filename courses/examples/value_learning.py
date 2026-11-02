"""用 CPU 表格 Q-learning 演示 TD 目标、自举与半梯度更新。"""

import torch

# 与上一节相同：状态 2 真正终止；其转移和奖励仅作占位。
NEXT_STATES = ((1, 0), (2, 0), (2, 2))
REWARDS = ((-1.0, -0.2), (2.0, -0.5), (0.0, 0.0))


def bootstrap_target(reward: torch.Tensor, next_value: torch.Tensor,
                     terminated: bool, gamma: float) -> torch.Tensor:
    """构造不参与求导的标量 TD 目标。

    Args:
        reward: 本次转移的标量奖励。
        next_value: 下一状态的价值估计，此处由当前 Q 表取最大值得到。
        terminated: 是否真正终止；仍可继续的时间截断应传 False。
        gamma: 折扣因子。

    Returns:
        真正终止时为奖励，否则为奖励加折扣后的下一价值。
    """
    # no_grad 只断开目标的梯度；这里没有独立或滞后的目标网络。
    with torch.no_grad():
        return reward + gamma * (not terminated) * next_value


def learn_q(gamma: float,
            steps: int = 6000) -> tuple[torch.Tensor, torch.Tensor]:
    """从在线 epsilon-greedy 交互样本逐步更新 Q 表。

    Args:
        gamma: 折扣因子。
        steps: 总交互次数，真正终止后从状态 0 开始新回合。

    Returns:
        形状均为 [3, 2] 的 Q 表与各状态动作的访问次数。
    """
    action_values = torch.nn.Parameter(
        torch.zeros((3, 2), dtype=torch.float64, device="cpu"))
    optimizer = torch.optim.SGD([action_values], lr=0.1)
    visits = torch.zeros((3, 2), dtype=torch.int64, device="cpu")
    state = 0
    for _ in range(steps):
        # 行为策略保留 25% 随机探索，目标策略始终取最大 Q。
        if torch.rand((), device="cpu").item() < 0.25:
            action = torch.randint(2, (), device="cpu").item()
        else:
            action = action_values[state].argmax().item()
        next_state = NEXT_STATES[state][action]
        terminated = next_state == 2
        target = bootstrap_target(
            action_values.new_tensor(REWARDS[state][action]),
            action_values[next_state].max(), terminated, gamma)
        loss = 0.5 * (action_values[state, action] - target).square()
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        visits[state, action] += 1
        state = 0 if terminated else next_state
    # Pylint 无法推断 Parameter.detach；实际调用与输出由下方断言验证。
    # pylint: disable-next=not-callable
    return action_values.detach(), visits


def main() -> None:
    """验证 TD 热身、终止掩码、解析最优价值与贪心执行回报。"""
    torch.manual_seed(7)
    torch.set_printoptions(precision=8)
    gamma = 0.9
    reward = torch.tensor(-1.0, dtype=torch.float64, device="cpu")
    warmup_target = bootstrap_target(reward, reward.new_tensor(1.5), False,
                                     gamma)
    updated_value = 0.0 + 0.2 * (warmup_target - 0.0)
    torch.testing.assert_close(warmup_target, reward.new_tensor(0.35))
    torch.testing.assert_close(updated_value, reward.new_tensor(0.07))

    # 故意用非零下一价值，避免终止态恰好为零掩盖漏写掩码的错误。
    nonzero_next = torch.tensor(7.0,
                                dtype=torch.float64,
                                device="cpu",
                                requires_grad=True)
    terminal_target = bootstrap_target(reward, nonzero_next, True, gamma)
    truncated_target = bootstrap_target(reward, nonzero_next, False, gamma)
    torch.testing.assert_close(terminal_target, reward)
    torch.testing.assert_close(truncated_target, reward.new_tensor(5.3))
    assert not terminal_target.requires_grad
    assert not truncated_target.requires_grad

    action_values, visits = learn_q(gamma)
    exact_values = action_values.new_tensor([[0.8, 0.52], [2.0, 0.22],
                                             [0.0, 0.0]])
    torch.testing.assert_close(action_values, exact_values, atol=1e-6, rtol=0)
    assert visits[:2].min().item() >= 100
    assert visits[2].sum().item() == 0
    greedy_actions = action_values[:2].argmax(dim=1)
    assert greedy_actions.tolist() == [0, 0]

    state = 0
    greedy_return = 0.0
    for step in range(10):
        action = greedy_actions[state].item()
        greedy_return += gamma**step * REWARDS[state][action]
        state = NEXT_STATES[state][action]
        if state == 2:
            break
    assert state == 2
    assert abs(greedy_return - 0.8) < 1e-12

    print(f"TD 热身：target={warmup_target.item():.2f}，"
          f"更新后 V(0)={updated_value.item():.2f}")
    print(f"终止目标={terminal_target.item():.1f}，"
          f"时间截断目标={truncated_target.item():.1f}")
    print(f"gamma={gamma}, dtype={action_values.dtype}, "
          f"device={action_values.device}")
    print(f"各状态动作访问次数=\n{visits}")
    print(f"学习得到 Q=\n{action_values}")
    print(f"与解析 Q* 最大误差="
          f"{(action_values - exact_values).abs().max().item():.3e}")
    print(f"贪心动作={greedy_actions.tolist()}，"
          f"从状态 0 执行的回报={greedy_return:.2f}")


if __name__ == "__main__":
    main()
