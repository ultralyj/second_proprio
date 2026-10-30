"""在极简控制问题中运行 DAgger 的采集、标注、聚合与训练。"""

import torch

import bc_shift


def main() -> None:
    """比较行为克隆与三轮数据聚合后的恢复能力。"""
    torch.manual_seed(7)
    policy = bc_shift.make_policy()
    states = torch.zeros(16, 1)
    actions = bc_shift.expert(states)
    bc_shift.fit(policy, states, actions)
    before = bc_shift.rollout(policy)[-1].item()
    print(f"BC第11步偏差={before:.6f}")
    for iteration in range(3):
        # 这里让学习者独立执行，等价于这几轮的专家混合权重为零。
        visited = bc_shift.rollout(policy)
        labels = bc_shift.expert(visited)
        states = torch.cat((states, visited))
        actions = torch.cat((actions, labels))
        loss = bc_shift.fit(policy, states, actions)
        final_state = bc_shift.rollout(policy)[-1].item()
        print(f"轮次={iteration + 1}, 样本数={len(states)}, "
              f"聚合MSE={loss:.6f}, 第11步偏差={final_state:.6f}")


if __name__ == "__main__":
    main()
