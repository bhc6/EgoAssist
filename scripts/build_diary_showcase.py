"""Build a portable review page using only actual saved diary results."""
import argparse
import html
import json
from pathlib import Path


def main(folder):
    report=json.loads((folder/'verification.json').read_text('utf-8'))
    assert report['passed'] and report['mock'] is False
    esc=html.escape;cards=[]
    labels={'overview':'这段时间','life':'生活','work':'工作','changes':'进展与变化','pending':'还没完成的事','suggestions':'下一步可以做什么'}
    for kind,title in [('daily','日记'),('weekly','周回顾'),('monthly','月回顾')]:
        row=json.loads((folder/(kind+'.json')).read_text('utf-8'));body=row['body']
        contents=''.join('<h3>'+label+'</h3>'+''.join('<p>'+esc(item['text'])+'</p>' for item in body[key]) for key,label in labels.items() if body[key])
        cards.append(f'<section id="{kind}"><h2>{title} · {esc(body["title"])}</h2><p class="note">范围：{esc(row["period"]["start"][:10])} 至 {esc(row["period"]["end"][:10])}（结束日不含）；{len(row["calls"])} 次真实模型处理。</p>{contents}<p><a href="{kind}.json">原始结果、来源与模型请求记录</a></p><details><summary>查看实际应用截图</summary><img src="{kind}-desktop.png" loading="lazy" alt="{title}实际应用截图"></details></section>')
    videos=sorted((folder/'recordings').glob('*.webm'),key=lambda path:path.stat().st_size,reverse=True)
    recording=report.get('walkthroughRecording') or report.get('browserRecording') or (videos[0].relative_to(folder).as_posix() if videos else None)
    video=(f'<h2>实际页面操作录屏</h2><p class="note">浏览真实归档、展开来源、按日期查阅和反馈；无音轨。生成与恢复结果见实测记录。</p><video controls preload="metadata" src="{esc(recording)}"></video>' if recording else '')
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>个人日记 · 实际功能展示</title><style>*{box-sizing:border-box}body{margin:0;background:#f5f4ef;color:#243c35;font:17px/1.85 system-ui,'Microsoft YaHei',sans-serif}main{max-width:1050px;margin:auto;padding:30px}h1{font-size:34px}h2{font-size:25px}h3{font-size:19px;margin-top:26px}section{background:#fffef9;border:1px solid #dee3d9;border-radius:14px;padding:26px;margin:26px 0}nav{display:flex;gap:20px;flex-wrap:wrap}a{color:#28634f}.note{font-size:14px;color:#627766}details{margin:20px 0}summary{cursor:pointer}img,video{width:100%;height:auto}p{overflow-wrap:anywhere}@media(max-width:600px){main{padding:16px}section{padding:18px}h1{font-size:28px}}</style><main><p>EG OASSIST / 独立个人日记功能</p><h1>留住经历，看见变化</h1><p>日记、周回顾和月回顾均由当前系统实际生成，按生活、工作、变化、待办和建议组织。每条有来源，可补记、反馈和导出。</p><nav><a href="http://127.0.0.1:8779/diary.html">打开实际日记应用</a><a href="#daily">日记</a><a href="#weekly">周回顾</a><a href="#monthly">月回顾</a><a href="verification.json">实测记录</a></nav><p class="note">本次使用之前保存的真实系统演示日志，包含脚本化对话与 EgoLife 重放画面。实际只有 2026-09-16 的记录，不是连续一周或一个月的真人生活数据；未记录日期不作推断。内容是原始模型输出，不是人工编写的日记样例。</p>'''
    page+='<p class="note">本机演示账号：diary-demo；密码：Diary-local-demo-20260917。月回顾首次汇总超时，重启后通过页面重试成功，复用 5 个已完成结果，仅增加 1 次模型处理。<a href="verification-before-resume.json">首次失败</a> · <a href="monthly-before-resume.json">超时检查点</a> · <a href="../../docs/DIARY_IMPLEMENTATION_2026-09-17.md">实现与验收说明</a></p><p class="note">内容质量仍需人工核对：本次模型将部分“已交付草稿、尚待用户确认”的事项归入未完成；EgoLife 回答只是系统当时的回答，并非正确答案验证。引用有效不等于语义完全正确。</p>'
    page+=video+''.join(cards)+'<details><summary>手机阅读页面</summary><img style="max-width:390px" src="monthly-mobile.png" loading="lazy" alt="手机阅读页面"></details></main></html>'
    (folder/'index.html').write_text(page.replace('EG OASSIST','EGOASSIST'),encoding='utf-8')
    print(folder/'index.html')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True)
    main(parser.parse_args().source.resolve())
