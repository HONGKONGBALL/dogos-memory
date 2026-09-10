"""Deterministic companion policy. No hardware, network or wall-clock dependencies."""
from dataclasses import dataclass, asdict


@dataclass
class Config:
    response_window: float = 12
    interaction_window: float = 10
    invitation_cooldown: float = 60
    max_cooldown: float = 600
    observation_ttl: float = 30
    greeting_enabled: bool = True
    arrival_stability: float = 2
    greeting_window: float = 3
    absence_rearm: float = 30
    invitation_budget: int = 2
    budget_window: float = 600
    proactive_budget: int = 4
    idle_interval: float = 90
    rest_window: float = 60


class Companion:
    def __init__(self, config=None, memory=None):
        self.config = config or Config()
        self.state = 'paused'
        self.reason = '尚未启动'
        self.present = False
        self.busy = False
        self.charging = False
        self.battery = None
        self.seen_at = float('-inf')
        self.deadline = 0
        self.next_invitation = 0
        self.ignored = min(10, max(0, int((memory or {}).get('ignored', 0))))
        self.interactions = max(0, int((memory or {}).get('interactions', 0)))
        self.fault = False
        self.preference = (memory or {}).get('preference', 'free')
        if self.preference not in ('free', 'quiet', 'rest'):
            self.preference = 'free'
        self.social_need = .65
        self.curiosity = .45
        self.fatigue = .1
        self.last_tick = None
        self.present_since = None
        self.absent_since = None
        self.greeting_pending = True
        self.last_touch_response = float('-inf')
        self.last_idle = 0
        self.invitations = []
        self.proactive = []
        self.rest_until = 0
        self.decisions = []

    def memory(self):
        return {'ignored': self.ignored, 'interactions': self.interactions, 'preference': self.preference}

    def snapshot(self):
        return {**self.memory(), 'state': self.state, 'reason': self.reason,
                'present': self.present, 'busy': self.busy,
                'charging': self.charging, 'battery': self.battery,
                'drives': {'social_need':round(self.social_need, 2), 'curiosity':round(self.curiosity, 2), 'fatigue':round(self.fatigue, 2)},
                'drive_note':'行为设计变量，不代表真实情绪或生理需求',
                'decisions': self.decisions[-12:],
                'config': asdict(self.config)}

    def change(self, state, reason):
        self.state, self.reason = state, reason

    def choose(self, action, state, reason, now):
        self.change(state, reason)
        self.decisions.append({'action':action, 'reason':reason})
        self.decisions = self.decisions[-12:]
        self.fatigue = min(1, self.fatigue + .04)
        self.last_idle = now
        if action != 'acknowledge':
            self.proactive.append(now)
        return [action]

    def advance(self, now):
        if self.last_tick is not None and self.state not in ('paused', 'fault'):
            # Do not accumulate motivation during absent/stale telemetry or process suspension.
            dt = max(0, min(now-self.last_tick, 10))
            if now-self.seen_at < self.config.observation_ttl:
                self.social_need = min(1, self.social_need + dt*.0008)
                self.curiosity = min(1, self.curiosity + dt*.002)
                self.fatigue = max(0, self.fatigue - dt*(.005 if self.state=='resting' else .001))
        self.last_tick = now

    def event(self, kind, now, data=None):
        data = data or {}
        self.advance(now)
        if kind == 'preference':
            if data.get('mode') not in ('free','quiet','rest'):
                raise ValueError('Unknown companionship preference')
            self.preference = data['mode']
            if self.state not in ('paused','fault'):
                self.change('resting' if self.preference=='rest' else 'quiet', '已应用陪伴偏好')
            return []
        if kind == 'pause':
            self.change('paused', '用户暂停自主行为')
            return []
        if kind == 'start':
            if self.fault or self.state != 'paused':
                return []
            self.change('quiet', '陪伴模式已启动，等待新鲜的环境输入')
            return self.tick(now)
        if kind == 'reset_fault':
            self.fault = False
            self.change('paused', '故障已确认；需重新启动')
            return []
        if kind == 'failure':
            self.fault = True
            self.change('fault', '执行未确认，暂停后续行为，避免重试造成重复动作')
            return []
        if kind == 'observation':
            was_present = self.present
            self.present = data['present']
            self.busy = data['busy']
            self.charging = data['charging']
            self.battery = data['battery']
            self.seen_at = now
            if self.present:
                if not was_present:
                    self.present_since = now
                    if self.absent_since is not None and now-self.absent_since >= self.config.absence_rearm:
                        self.greeting_pending = True
                self.absent_since = None
            else:
                self.present_since = None
                if self.absent_since is None:
                    self.absent_since = now
        if self.state in ('paused', 'fault'):
            return []
        blocked = self.blocked(now)
        if blocked:
            self.change('resting' if self.charging or (self.battery is not None and self.battery<=25) or self.preference=='rest' else 'quiet', blocked)
            return []
        if kind in ('respond', 'touch'):
            if self.preference != 'free' or now-self.last_touch_response < 6:
                return []
            if self.state != 'inviting' and not (kind == 'touch' and self.state in ('quiet','attending','resting')):
                return []
            if self.state == 'inviting' and now >= self.deadline:
                return self.tick(now)
            self.interactions += 1
            self.ignored = 0
            self.last_touch_response = now
            self.social_need = max(0, self.social_need-.35)
            self.fatigue = min(1, self.fatigue+.08)
            self.greeting_pending = False
            self.deadline = now + self.config.interaction_window
            self.next_invitation = now + self.config.invitation_cooldown
            return self.choose('acknowledge', 'interacting', '收到回应，短暂互动后安静陪伴', now)
        return self.tick(now)

    def blocked(self, now):
        if now - self.seen_at >= self.config.observation_ttl:
            return '环境输入已过期，等待更新'
        if self.charging:
            return '正在充电，保持安静'
        if self.battery is None or self.battery <= 25:
            return '电量未知或偏低，保持安静'
        if self.preference == 'rest':
            return '主人要求休息，等待明确恢复自由陪伴'
        if self.preference == 'quiet':
            return '主人要求安静，暂停主动邀请和小动作'
        if not self.present:
            return '没有人在场，保持安静'
        if self.busy:
            return '对方在忙，暂不打扰'
        return None

    def tick(self, now):
        self.advance(now)
        if self.state in ('paused', 'fault'):
            return []
        blocked = self.blocked(now)
        if blocked:
            self.change('resting' if self.charging or (self.battery is not None and self.battery<=25) or self.preference=='rest' else 'quiet', blocked)
            return []
        if self.state == 'resting' and now < self.rest_until:
            self.change('resting','互动较多，先休息一会儿')
            return []
        if self.fatigue >= .75:
            self.rest_until = now+self.config.rest_window
            self.change('resting','互动较多，主动休息；没有调用趴下动作')
            return []
        if self.state == 'attending':
            if now < self.deadline:
                return []
            self.change('quiet','打过招呼，观察对方是否愿意互动')
        self.proactive = [t for t in self.proactive if now-t < self.config.budget_window]
        budget_available = len(self.proactive) < self.config.proactive_budget
        if self.config.greeting_enabled and self.greeting_pending:
            if not budget_available:
                self.change('quiet','主动互动次数已足够，继续安静陪伴')
                return []
            if self.present_since is None or now-self.present_since < self.config.arrival_stability:
                self.change('quiet','正在确认有人持续在场，避免一闪而过就打招呼')
                return []
            self.greeting_pending = False
            self.deadline = now+self.config.greeting_window
            return self.choose('greet','attending','有人持续出现，轻轻打一次招呼；尚未识别主人身份',now)
        if self.state == 'inviting':
            if now >= self.deadline:
                self.ignored = min(self.ignored + 1, 10)
                delay = min(self.config.max_cooldown,
                            self.config.invitation_cooldown * 2 ** self.ignored)
                self.next_invitation = now + delay
                self.change('quiet', f'没有收到回应，至少 {delay:g} 秒后再邀请')
            return []
        if self.state == 'interacting':
            if now >= self.deadline:
                self.change('quiet', '互动结束，安静陪伴')
            return []
        self.invitations = [t for t in self.invitations if now-t < self.config.budget_window]
        if budget_available and now >= self.next_invitation and self.social_need >= .6 and len(self.invitations) < self.config.invitation_budget:
            self.deadline = now + self.config.response_window
            self.next_invitation = now + min(self.config.max_cooldown,
                                            self.config.invitation_cooldown * 2 ** self.ignored)
            self.invitations.append(now)
            return self.choose('invite','inviting','人在场、愿意社交且打扰预算充足，邀请一次',now)
        if budget_available and not self.ignored and self.curiosity >= .7 and now-self.last_idle >= self.config.idle_interval:
            self.curiosity = .2
            return self.choose('look_around','quiet','已经安静陪伴一段时间，做一次轻微好奇动作',now)
        if self.ignored:
            self.change('quiet','刚才没有得到回应，继续安静陪伴，不用小动作催促')
        elif len(self.invitations) >= self.config.invitation_budget:
            self.change('quiet','本轮主动邀请次数已足够，继续安静陪伴')
        elif self.social_need < .6:
            self.change('quiet','刚得到陪伴和回应，满足地安静待着')
        else:
            self.change('quiet', '正在安静陪伴，尚未到下次邀请时间')
        return []
