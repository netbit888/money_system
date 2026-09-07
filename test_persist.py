"""测试持久化逻辑"""
import os
import json

# 清理旧数据（P2 起改为分层存储：L0 日志 + 归档）
for _p in ('snapshots/history-l0.jsonl', 'snapshots/history-archive.json'):
    if os.path.exists(_p):
        os.remove(_p)

from engine import Simulation

sim = Simulation()
sim.reset_round('config')
# 测试期间每回合都存盘（验证持久化逻辑）
sim._persist_every = 1

print('=== 跑回合直到人口为 0 或 25 回合 ===')
for i in range(25):
    if len(sim.persons) == 0:
        print(f'第 {sim.round} 回合人口为 0，停止')
        break
    sim.next_round()
    print(f'回合 {sim.round}: 人口 {len(sim.persons)}')

print()
print(f'当前回合: {sim.round}')
print(f'历史记录数: {len(sim.history)}')
if sim.history:
    print('前 3 行历史:')
    for h in sim.history[:3]:
        print(f'  round={h["round"]}, total={h["total"]}, groups={h["groups"]}')
    print(f'最后 1 行: round={sim.history[-1]["round"]}, total={sim.history[-1]["total"]}')

print()
hist_path = 'snapshots/history-l0.jsonl'
print(f'{hist_path} 存在: {os.path.exists(hist_path)}')
if os.path.exists(hist_path):
    with open(hist_path, 'r', encoding='utf-8') as f:
        data = [json.loads(line) for line in f if line.strip()]
    print(f'存盘记录数(L0 日志): {len(data)}')
    if data:
        print(f'存盘最后回合: {data[-1]["round"]}')
arch_path = 'snapshots/history-archive.json'
print(f'{arch_path} 存在: {os.path.exists(arch_path)}')
if os.path.exists(arch_path):
    with open(arch_path, 'r', encoding='utf-8') as f:
        arch = json.load(f)
    print(f'归档层数: {len(arch.get("levels", []))}，覆盖到回合: {arch.get("max_round")}')

print()
print('=== 模拟服务重启（重新加载历史）===')
sim2 = Simulation()
print(f'重载后历史记录数: {len(sim2.history)}')
if sim2.history:
    print(f'重载后最后回合: {sim2.history[-1]["round"]}')

print()
print('=== 测试 get_history 切片 ===')
if sim.history:
    n1 = len(sim.get_history(2, 5))
    n2 = len(sim.get_history(5))
    print(f'回合 2-5: {n1} 条')
    print(f'回合 5+: {n2} 条')

print()
print('=== 测试 reset 清空历史 ===')
sim.reset_round('config')
print(f'reset 后历史记录数: {len(sim.history)}')
print(f'{hist_path} 仍存在: {os.path.exists(hist_path)}')
