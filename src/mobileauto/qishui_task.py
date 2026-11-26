"""汽水音乐领时长：把 qishui_runner.py 的成熟状态机转成配置驱动 JSON 任务。

这样工作台和独立 bat 执行的是同一份任务 JSON：
  - 工作台「任务列表」里能直接看到、编辑、画流程图
  - bat 通过 MOBILEAUTO_TASK_KEY=qishui_time 走 app_task -> configured_flow
  - 独立脚本 qishui_runner.py 保留为参考实现（历史踩坑记录）

状态优先级与 qishui_runner.classify 完全一致：
  DONE > SPLASH > CONTINUE > REWARD > COUNTDOWN > LIVE > LANDING > SUCCESS > WELFARE > HOME > AD > UNKNOWN
"""

QISHUI_TASK = {
    "key": "qishui_time",
    "name": "汽水音乐-领时长（完整版）",
    "description": (
        "成熟状态机：跳过开屏→福利页→领时长→广告静音→倒计时秒点×→直播秒退→"
        "续看弹窗→领奖→成功尾页退出→循环；识别「明日再来」自动结束并输出畅听权益截止"
    ),
    "app": "com.luna.music",
    "enabled": True,
    "max_loops": 30,
    "engine": "configured",
    "poll": 0.25,
    "tap_gap": 0.35,
    "capabilities": {
        "ocr": True,
        "action_verify": True,
        "anti_stuck": True,
        "dedupe": True,
        "structured_log": True,
        "end_detect": True,
        "auto_start": True,
        "entitlement": True,
    },
    # 按优先级排列；match/click_target 把「判定条件」与「点击目标」解耦
    "states": [
        {
            "name": "DONE",
            "match": ["明日再来", "畅听权益截止", "已领完"],
            "action": "finish",
            "finish_title": "今日时长已刷满，任务结束",
            "finish_fallback": "明日再来/已领完，今日时长已刷满",
            "label": "结束检测（明日再来）",
        },
        {
            "name": "SPLASH",
            "match_all": [["跳过"], ["开通SVIP", "汽水音乐", "点击查看更多"]],
            "click_target": ["跳过"],
            "action": "click_match",
            "label": "跳过开屏",
        },
        {
            "name": "CONTINUE",
            "match": ["继续观看", "再看20秒得", "再看15秒得", "再看10秒得"],
            "click_target": ["继续观看"],
            "action": "click_match",
            "label": "继续观看",
        },
        {
            "name": "REWARD",
            "match_any": [
                {"match": ["领取奖励", "立即领取"], "exclude": ["秒后", "秒可"]},
                ["再看1个视频", "再看2个视频", "提前得", "坚持退出"],
            ],
            "click_target": ["领取奖励", "立即领取"],
            "click_exclude": ["秒后", "秒可"],
            "action": "click_match",
            "label": "领取奖励",
        },
        {
            "name": "COUNTDOWN",
            "match": ["秒后可领取", "秒可领取", "后可领取奖励"],
            "action": "mute_once",
            "ratio": [0.23, 0.027],
            "label": "倒计时（静音，倒计时消失秒点×）",
        },
        {
            "name": "LIVE",
            "match_any": [
                ["说点什么", "本场点赞", "更多直播"],
                {"all": ["已关注", "直播"]},
            ],
            "action": "click_ratio_once",
            "ratio": [0.938, 0.078],
            "label": "关直播×",
        },
        {
            "name": "LANDING",
            "match": ["点击进入直播间", "直播间观看时长计入"],
            "action": "close_ad",
            "ratio": [0.924, 0.027],
            "label": "关直播落地页",
        },
        {
            "name": "SUCCESS",
            "match": ["领取成功", "恭喜获得奖励", "优惠券已放入"],
            "action": "close_ad",
            "back_fallback": True,
            "label": "领取成功尾页退出",
        },
        {
            "name": "WELFARE",
            "match": ["领时长", "福利中心", "免费听时长", "已解锁"],
            "click_target": ["领时长", "再看1次", "再看2次"],
            "action": "click_match",
            "count": True,
            "retry": True,
            "reset_flags": ["_muted_AD", "_muted_COUNTDOWN", "_muted_UNKNOWN", "_once_LIVE"],
            "label": "领时长（新广告）",
        },
        {
            "name": "HOME",
            "match_all": [
                {"match": ["福利"], "exclude": ["福利中心"]},
                ["我的", "音乐"],
            ],
            "click_target": ["福利"],
            "click_exclude": ["福利中心"],
            "action": "click_match",
            "label": "进入福利页",
        },
        {
            "name": "AD",
            "match": ["广告", "反馈", "立即下载", "进入直播", "立即使用"],
            "action": "mute_once",
            "ratio": [0.23, 0.027],
            "label": "广告静音",
        },
        {
            "name": "UNKNOWN",
            "fallback": True,
            "action": "mute_once",
            "ratio": [0.23, 0.027],
            "label": "未知页静音兜底",
        },
    ],
    "stuck_states": ["SUCCESS", "LANDING", "LIVE"],
    "stuck_limit": 12,
    "finish_info_keywords": ["明日再来", "畅听权益截止", "已领完"],
}
