"""用专家数据未覆盖的恢复状态展示行为克隆的局限。"""

import torch


def expert(states: torch.Tensor) -> torch.Tensor:
    """返回把状态拉向零点的专家动作。"""
    return -0.5 * states


def make_policy() -> torch.nn.Linear:
    """创建初始斜率为正的最小连续动作策略。"""
    policy = torch.nn.Linear(1, 1, bias=False)
    with torch.no_grad():
        policy.weight.fill_(0.2)
    return policy


def fit(
    policy: torch.nn.Module,
    states: torch.Tensor,
    actions: torch.Tensor,
) -> float:
    """在给定状态和专家标签上拟合策略。

    Args:
        policy: 要更新的 PyTorch 策略。
        states: 形状为 N×1 的训练状态。
        actions: 形状为 N×1 的专家动作标签。

    Returns:
        更新后的训练均方误差。
    """
    optimizer = torch.optim.Adam(policy.parameters(), lr=0.03)
    for _ in range(200):
        loss = torch.mean((policy(states) - actions)**2)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
    return torch.mean((policy(states) - actions)**2).item()


def rollout(policy: torch.nn.Module,
            initial_state: float = 1.0) -> torch.Tensor:
    """收集策略在扰动起点下访问的十二个状态。

    Args:
        policy: 用于闭环控制的策略。
        initial_state: 扰动后的初始偏差。

    Returns:
        形状为 12×1 的访问状态，不包含最后一次动作后的状态。
    """
    state = torch.tensor([[initial_state]])
    visited = []
    with torch.no_grad():
        for _ in range(12):
            visited.append(state.clone())
            state = state + policy(state)
    return torch.cat(visited)


def main() -> None:
    """展示零训练误差仍不能保证未覆盖状态上的控制行为。"""
    torch.manual_seed(7)
    policy = make_policy()
    # 极简演示只收集专家已经稳定在目标处的状态，刻意暴露覆盖问题。
    expert_states = torch.zeros(16, 1)
    loss = fit(policy, expert_states, expert(expert_states))
    visited = rollout(policy)
    print(f"训练MSE={loss:.6f}, 策略斜率={policy.weight.item():.3f}")
    print(f"第0步偏差={visited[0].item():.3f}, 第11步偏差={visited[-1].item():.3f}")


if __name__ == "__main__":
    main()
