"""
多单位协同拦截动态目标系统 - FastAPI 后端服务

支持敌方多轨迹预测、时空路径规划、多智能体协同和拦截判定。
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib

# ============================================================================
# 数据模型
# ============================================================================

class State(BaseModel):
    """时空状态 (x, y, t)"""
    x: float
    y: float
    t: float

class Trajectory(BaseModel):
    """轨迹"""
    states: List[List[float]]  # [[x, y, t], ...]
    length: float

class PredictRequest(BaseModel):
    """预测请求"""
    enemy_start: Dict[str, float]
    enemy_speed: float
    enemy_theta: float
    predict_time: float

class PlanRequest(BaseModel):
    """规划请求"""
    agent_starts: List[Dict[str, float]]
    agent_speed: float
    enemy_trajectories: List[List[List[float]]]
    k_paths: int = 3

class InterceptRequest(BaseModel):
    """拦截请求"""
    agent_count: int
    agent_speed: float
    enemy_speed: float
    enemy_theta: float
    predict_time: float

# ============================================================================
# 缓存机制
# ============================================================================

class TrajectoryCache:
    """轨迹缓存"""
    def __init__(self, max_size=100):
        self.cache = {}
        self.max_size = max_size
        self.hits = 0
        self.misses = 0
    
    def _make_key(self, start, speed, duration):
        key_str = f"{start}_{speed}_{duration}"
        return hashlib.md5(key_str.encode()).hexdigest()
    
    def get(self, start, speed, duration):
        key = self._make_key(start, speed, duration)
        if key in self.cache:
            self.hits += 1
            return self.cache[key]
        self.misses += 1
        return None
    
    def set(self, start, speed, duration, trajectory):
        if len(self.cache) >= self.max_size:
            # 移除最旧的缓存
            self.cache.pop(next(iter(self.cache)))
        key = self._make_key(start, speed, duration)
        self.cache[key] = trajectory
    
    def get_stats(self):
        total = self.hits + self.misses
        hit_rate = (self.hits / total * 100) if total > 0 else 0
        return {
            'hits': self.hits,
            'misses': self.misses,
            'hit_rate': f'{hit_rate:.1f}%',
            'size': len(self.cache)
        }

# ============================================================================
# 预测模块
# ============================================================================

def predict_constant_velocity(start_x, start_y, start_t, speed, theta, duration, dt=0.1):
    """匀速直线运动预测"""
    trajectory = []
    t = start_t
    x, y = start_x, start_y
    
    while t <= start_t + duration:
        trajectory.append([x, y, t])
        x += speed * math.cos(theta) * dt
        y += speed * math.sin(theta) * dt
        t += dt
    
    return trajectory

def predict_coordinated_turn(start_x, start_y, start_t, speed, theta, omega, duration, dt=0.1):
    """协同转弯运动预测"""
    trajectory = []
    t = start_t
    x, y = start_x, start_y
    current_theta = theta
    
    while t <= start_t + duration:
        trajectory.append([x, y, t])
        x += speed * math.cos(current_theta) * dt
        y += speed * math.sin(current_theta) * dt
        current_theta += omega * dt
        t += dt
    
    return trajectory

class EnemyPredictor:
    """敌方多轨迹预测器"""
    def __init__(self, cache=None):
        self.cache = cache or TrajectoryCache()
    
    def predict(self, enemy_start, enemy_speed, enemy_theta, predict_time):
        """生成三条预测轨迹（并行）"""
        start_key = (enemy_start['x'], enemy_start['y'], enemy_start['t'])
        
        # 检查缓存
        cached = self.cache.get(start_key, enemy_speed, predict_time)
        if cached:
            return cached
        
        # 并行生成三条轨迹
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(predict_constant_velocity, 
                    enemy_start['x'], enemy_start['y'], enemy_start['t'],
                    enemy_speed, enemy_theta, predict_time),
                executor.submit(predict_coordinated_turn,
                    enemy_start['x'], enemy_start['y'], enemy_start['t'],
                    enemy_speed, enemy_theta, 0.2, predict_time),
                executor.submit(predict_coordinated_turn,
                    enemy_start['x'], enemy_start['y'], enemy_start['t'],
                    enemy_speed, enemy_theta, -0.2, predict_time),
            ]
            trajectories = [f.result() for f in as_completed(futures)]
        
        # 缓存结果
        self.cache.set(start_key, enemy_speed, predict_time, trajectories)
        
        return trajectories

# ============================================================================
# 规划模块
# ============================================================================

def calculate_distance(p1, p2):
    """计算欧几里得距离"""
    return math.sqrt((p1[0] - p2[0])**2 + (p1[1] - p2[1])**2)

def find_interception_point(agent_start, agent_speed, enemy_trajectory):
    """找到拦截点"""
    for i, enemy_state in enumerate(enemy_trajectory):
        ex, ey, et = enemy_state
        # 计算我方到达该点所需时间
        dist = calculate_distance(agent_start, [ex, ey])
        t_self = dist / agent_speed if agent_speed > 0 else float('inf')
        
        # 判断是否能拦截（时间误差 < 1.0s）
        if abs(t_self - et) < 1.0 and dist < 100:  # 距离阈值 100
            return [ex, ey, et], t_self
    
    return None, None

class PathPlanner:
    """时空路径规划器"""
    def plan_k_paths(self, agent_start, agent_speed, enemy_trajectory, k=3):
        """规划 K 条路径"""
        paths = []
        
        for _ in range(k):
            interception_point, t_self = find_interception_point(
                agent_start, agent_speed, enemy_trajectory
            )
            
            if interception_point:
                # 生成路径（简化：直线）
                path = [
                    [agent_start[0], agent_start[1], agent_start[2]],
                    interception_point,
                ]
                
                # 计算路径长度
                length = calculate_distance(agent_start, interception_point)
                
                paths.append({
                    'path': path,
                    'intercepted': True,
                    'interception_point': interception_point,
                    'length': length,
                })
            else:
                # 无法拦截的路径
                paths.append({
                    'path': [[agent_start[0], agent_start[1], agent_start[2]]],
                    'intercepted': False,
                    'interception_point': None,
                    'length': 0,
                })
        
        return paths

# ============================================================================
# 评分模块
# ============================================================================

class Evaluator:
    """路径评分器"""
    def evaluate(self, path_info, enemy_trajectory):
        """评分路径"""
        score = 0
        
        # 准则 1：拦截成功（50%）
        if path_info['intercepted']:
            score += 500
        
        # 准则 2：拦截时间（30%）
        if path_info['intercepted']:
            interception_point = path_info['interception_point']
            t_intercept = interception_point[2]
            time_penalty = max(0, 300 - t_intercept * 30)
            score += time_penalty
        
        # 准则 3：路径长度（20%）
        length_penalty = max(0, 200 - path_info['length'])
        score += length_penalty
        
        return score

# ============================================================================
# FastAPI 应用
# ============================================================================

app = FastAPI(title="多单位协同拦截系统", version="1.0.0")

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局对象
cache = TrajectoryCache(max_size=100)
predictor = EnemyPredictor(cache)
planner = PathPlanner()
evaluator = Evaluator()

# 性能监控
performance_stats = {
    'prediction': {'count': 0, 'total': 0, 'min': float('inf'), 'max': 0},
    'planning': {'count': 0, 'total': 0, 'min': float('inf'), 'max': 0},
    'evaluation': {'count': 0, 'total': 0, 'min': float('inf'), 'max': 0},
}

def record_performance(module_name, duration):
    """记录性能指标"""
    stats = performance_stats[module_name]
    stats['count'] += 1
    stats['total'] += duration
    stats['min'] = min(stats['min'], duration)
    stats['max'] = max(stats['max'], duration)

# ============================================================================
# API 端点
# ============================================================================

@app.get("/health")
async def health_check():
    """健康检查"""
    return {
        "status": "healthy",
        "service": "多单位协同拦截系统",
        "version": "1.0.0"
    }

@app.post("/predict")
async def predict(request: PredictRequest):
    """敌方多轨迹预测"""
    start_time = time.time()
    
    trajectories = predictor.predict(
        request.enemy_start,
        request.enemy_speed,
        request.enemy_theta,
        request.predict_time
    )
    
    duration = time.time() - start_time
    record_performance('prediction', duration)
    
    return {
        "trajectories": trajectories,
        "duration": duration
    }

@app.post("/plan")
async def plan(request: PlanRequest):
    """时空路径规划"""
    start_time = time.time()
    
    results = {}
    for i, agent_start in enumerate(request.agent_starts):
        # 为每个 Agent 规划路径
        agent_start_list = [agent_start['x'], agent_start['y'], agent_start['t']]
        
        paths = []
        for enemy_traj in request.enemy_trajectories:
            agent_paths = planner.plan_k_paths(
                agent_start_list,
                request.agent_speed,
                enemy_traj,
                request.k_paths
            )
            paths.extend(agent_paths)
        
        # 评分并排序
        scored_paths = []
        for path_info in paths[:request.k_paths]:
            score = evaluator.evaluate(path_info, request.enemy_trajectories[0])
            path_info['score'] = score
            scored_paths.append(path_info)
        
        scored_paths.sort(key=lambda x: x['score'], reverse=True)
        results[str(i)] = scored_paths[:request.k_paths]
    
    duration = time.time() - start_time
    record_performance('planning', duration)
    
    return {
        "results": results,
        "duration": duration
    }

@app.post("/intercept")
async def intercept(request: InterceptRequest):
    """完整拦截仿真"""
    start_time = time.time()
    
    # 预测敌方轨迹
    predict_start = time.time()
    enemy_start = {'x': 50.0, 'y': 10.0, 't': 0.0}
    trajectories = predictor.predict(
        enemy_start,
        request.enemy_speed,
        request.enemy_theta,
        request.predict_time
    )
    predict_duration = time.time() - predict_start
    record_performance('prediction', predict_duration)
    
    # 规划路径
    plan_start = time.time()
    top_paths = {}
    
    for agent_id in range(request.agent_count):
        # 初始位置
        if agent_id == 0:
            agent_start = {'x': 10.0, 'y': 50.0, 't': 0.0}
        else:
            agent_start = {'x': 90.0, 'y': 50.0, 't': 0.0}
        
        agent_start_list = [agent_start['x'], agent_start['y'], agent_start['t']]
        
        paths = []
        for enemy_traj in trajectories:
            agent_paths = planner.plan_k_paths(
                agent_start_list,
                request.agent_speed,
                enemy_traj,
                3
            )
            paths.extend(agent_paths)
        
        # 评分
        eval_start = time.time()
        scored_paths = []
        for path_info in paths[:3]:
            score = evaluator.evaluate(path_info, trajectories[0])
            path_info['score'] = score
            scored_paths.append(path_info)
        
        eval_duration = time.time() - eval_start
        record_performance('evaluation', eval_duration)
        
        scored_paths.sort(key=lambda x: x['score'], reverse=True)
        top_paths[str(agent_id)] = scored_paths[:3]
    
    plan_duration = time.time() - plan_start
    record_performance('planning', plan_duration)
    
    # 构建响应
    total_time = time.time() - start_time
    
    return {
        "success": True,
        "top_paths": top_paths,
        "enemy_trajectories": [
            {
                "states": traj,
                "length": sum(calculate_distance(traj[i], traj[i+1]) 
                            for i in range(len(traj)-1))
            }
            for traj in trajectories
        ],
        "total_time": total_time,
        "performance": {
            "prediction": {
                "count": performance_stats['prediction']['count'],
                "avg": performance_stats['prediction']['total'] / max(1, performance_stats['prediction']['count']),
                "min": performance_stats['prediction']['min'],
                "max": performance_stats['prediction']['max'],
            },
            "planning": {
                "count": performance_stats['planning']['count'],
                "avg": performance_stats['planning']['total'] / max(1, performance_stats['planning']['count']),
                "min": performance_stats['planning']['min'],
                "max": performance_stats['planning']['max'],
            },
            "evaluation": {
                "count": performance_stats['evaluation']['count'],
                "avg": performance_stats['evaluation']['total'] / max(1, performance_stats['evaluation']['count']),
                "min": performance_stats['evaluation']['min'],
                "max": performance_stats['evaluation']['max'],
            },
        },
        "cache_stats": cache.get_stats(),
        "pruned_paths": 0,
    }

@app.post("/benchmark")
async def benchmark():
    """性能基准测试"""
    times = []
    
    for _ in range(5):
        request = InterceptRequest(
            agent_count=2,
            agent_speed=15.0,
            enemy_speed=5.0,
            enemy_theta=1.57,
            predict_time=10.0
        )
        
        start = time.time()
        await intercept(request)
        times.append(time.time() - start)
    
    return {
        "benchmark": {
            "avg_time": sum(times) / len(times),
            "min_time": min(times),
            "max_time": max(times),
            "times": times,
        },
        "message": "性能测试完成"
    }

# ============================================================================
# 启动
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
