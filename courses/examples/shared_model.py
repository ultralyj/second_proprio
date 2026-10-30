"""用同一个一维控制问题展示行为克隆损失与强化学习回报。"""

import torch


def evaluate(gain: float) -> tuple[float, float, float]:
    """计算指定比例策略的监督误差、折扣回报和最终偏差。

    Args:
        gain: 策略 a = gain * s 的比例系数。

    Returns:
        监督均方误差、累计折扣回报以及最终状态偏差。
    """
    states = torch.linspace(-1.0, 1.0, 21).reshape(-1, 1)
    expert_actions = -0.5 * states
    predicted_actions = gain * states
    bc_loss = torch.mean((predicted_actions - expert_actions)**2)
    state = torch.tensor([1.0])
    discounted_return = torch.tensor(0.0)
    for step in range(12):
        action = gain * state
        reward = -(state.square() + 0.01 * action.square()).sum()
        discounted_return = discounted_return + 0.95**step * reward
        state = state + action
    return bc_loss.item(), discounted_return.item(), state.item()


def main() -> None:
    """比较收敛策略与会扩大偏差的策略。"""
    torch.manual_seed(7)
    for gain in (-0.5, 0.2):
        loss, total_return, final_state = evaluate(gain)
        print(f"gain={gain:+.1f}, BC损失={loss:.6f}, "
              f"回报={total_return:.6f}, 最终偏差={final_state:.6f}")


if __name__ == "__main__":
    main()
