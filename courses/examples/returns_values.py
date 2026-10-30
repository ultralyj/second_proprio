"""用三状态控制问题连接轨迹回报、状态价值与 Bellman 期望方程。"""

import torch


def discounted_returns(rewards: torch.Tensor, gamma: float) -> torch.Tensor:
    """从真正终止的轨迹末端反向累加折扣回报。

    Args:
        rewards: 按时间排列的一维奖励张量。
        gamma: 折扣因子。

    Returns:
        每个时间步的实际轨迹回报，形状与 rewards 相同。
    """
    returns = torch.zeros_like(rewards)
    running_return = rewards.new_zeros(())
    for step in range(len(rewards) - 1, -1, -1):
        running_return = rewards[step] + gamma * running_return
        returns[step] = running_return
    return returns


def make_mdp() -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """构建 CPU 上的双精度转移概率、奖励和固定策略。

    Returns:
        transitions[s, a, s_next]、rewards[s, a]、policy[s, a]。
    """
    # 状态：0 偏斜、1 对齐、2 真正终止；动作：0 主动调整/完成、1 等待。
    next_state = torch.tensor([[1, 0], [2, 0], [2, 2]], device="cpu")
    transitions = torch.eye(3, dtype=torch.float64, device="cpu")[next_state]
    rewards = torch.tensor([[-1.0, -0.2], [2.0, -0.5], [0.0, 0.0]],
                           dtype=torch.float64,
                           device="cpu")
    # 终止态的动作概率只用于吸收状态的矩阵占位；此后不再产生奖励。
    policy = torch.full((3, 2), 0.5, dtype=torch.float64, device="cpu")
    return transitions, rewards, policy


def evaluate_policy(
        transitions: torch.Tensor,
        rewards: torch.Tensor,
        policy: torch.Tensor,
        gamma: float,
        tolerance: float = 1e-12,
        max_iterations: int = 1000) -> tuple[torch.Tensor, torch.Tensor, int]:
    """迭代 Bellman 期望备份，计算固定策略的状态价值和动作价值。

    Args:
        transitions: 形状为 [状态数, 动作数, 状态数] 的转移概率。
        rewards: 形状为 [状态数, 动作数] 的期望即时奖励。
        policy: 形状为 [状态数, 动作数] 的固定动作概率。
        gamma: 小于 1 的非负折扣因子。
        tolerance: 相邻两轮状态价值的最大允许差值。
        max_iterations: 最大迭代次数。

    Returns:
        状态价值、动作价值及实际迭代次数。

    Raises:
        RuntimeError: 达到最大迭代次数仍未收敛。
    """
    values = rewards.new_zeros(rewards.shape[0])
    for iteration in range(1, max_iterations + 1):
        action_values = rewards + gamma * (transitions @ values)
        updated_values = (policy * action_values).sum(dim=1)
        change = (updated_values - values).abs().max().item()
        values = updated_values
        if change < tolerance:
            # 使用最终 V 重算 Q，便于检查两条 Bellman 关系。
            action_values = rewards + gamma * (transitions @ values)
            return values, action_values, iteration
    raise RuntimeError("固定策略评价在指定迭代次数内未收敛。")


def main() -> None:
    """打印轨迹回报与策略价值，并用线性方程交叉验证。"""
    torch.manual_seed(7)
    torch.set_printoptions(precision=8)
    gamma = 0.9
    trajectory_rewards = torch.tensor([-1.0, 2.0],
                                      dtype=torch.float64,
                                      device="cpu")
    returns = discounted_returns(trajectory_rewards, gamma)
    torch.testing.assert_close(returns, returns.new_tensor([0.8, 2.0]))
    transitions, rewards, policy = make_mdp()
    values, action_values, iterations = evaluate_policy(transitions, rewards,
                                                        policy, gamma)

    # 将动作按固定策略求期望，得到 V = r_pi + gamma * P_pi @ V。
    policy_transition = (policy.unsqueeze(-1) * transitions).sum(dim=1)
    policy_rewards = (policy * rewards).sum(dim=1)
    identity = torch.eye(3, dtype=torch.float64, device="cpu")
    # Pylint 无法推断此 PyTorch 动态函数；实际调用及结果由下方断言验证。
    # pylint: disable-next=not-callable
    exact_values = torch.linalg.solve(identity - gamma * policy_transition,
                                      policy_rewards)
    bellman_backup = policy_rewards + gamma * (policy_transition @ values)
    residual = (values - bellman_backup).abs().max().item()
    policy_average = (policy * action_values).sum(dim=1)
    consistency_error = (values - policy_average).abs().max().item()
    torch.testing.assert_close(values, exact_values, atol=1e-10, rtol=0)
    torch.testing.assert_close(values, policy_average, atol=1e-10, rtol=0)
    assert values[2].item() == 0.0
    assert torch.equal(action_values[2], action_values.new_zeros(2))

    print(f"gamma={gamma}, dtype={values.dtype}, device={values.device}")
    print(f"轨迹奖励={trajectory_rewards.tolist()}")
    print(f"反向计算 return={returns.tolist()}")
    print(f"固定策略迭代 V={values}")
    print(f"固定策略迭代 Q=\n{action_values}")
    print(f"线性方程解 V={exact_values}")
    print(f"Bellman 最大残差={residual:.3e}, 迭代次数={iterations}")
    print(f"V 与 sum_a pi(a|s) Q(s,a) 最大差值={consistency_error:.3e}")
    print(f"真正终止状态 V(2)={values[2].item():.1f}")


if __name__ == "__main__":
    main()
