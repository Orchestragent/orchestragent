#!/bin/zsh
cd "${0:A:h:h}" || exit 1
export PYTHONPATH="$PWD/src"
while true; do
  print '\nOrchestragent 官方登录与配置'
  print '1 Claude Code（待登录，继续独立审查）'
  print '2 Cursor（登录准备；后台执行尚未开放）'
  print '3 Kimi（仅官方人工交互）'
  print '4 DSH（API 密钥与独立预算，不会自动消费）'
  print '5 Jev（Vercel AI Gateway 密钥与独立预算，只供 orch 分诊）'
  print '0 退出；Codex 已登录，无需重复登录'
  read 'choice?选择 [1]: '
  case "${choice:-1}" in
    1) provider=claude ;;
    2) provider=cursor ;;
    3) provider=kimi ;;
    4) provider=dsh ;;
    5) provider=jev ;;
    0) exit 0 ;;
    *) continue ;;
  esac
  .venv/bin/python -m orchestragent.setup "$provider"
done
