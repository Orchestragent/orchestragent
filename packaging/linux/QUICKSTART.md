# orch（Linux 版）快速开始

`orch` 是一个本地的多 agent 开发编排 CLI：Claude 规划并冻结测试，DeepSeek V4.1 Flash（DSF）
并行实现，确定性代码验证、合并到本地 `orch/<run>` 分支。它不推送、不切换账号或计费方式。
完整说明见 `docs/ORCH.md`。

## 1. 解包与安装（一次，不需要 root、不需要联网）

```sh
tar -xzf orch-linux.tar.gz
cd orch-linux
./install.sh
```

`install.sh` 用系统 Python（3.12+，必须在 `/usr` 下，测试沙箱只挂载 `/usr`、`/bin`、`/lib`）
创建 `.venv`，并离线安装包里自带的 tree-sitter（按 `uv.lock` 校验哈希）。最后逐项检查
git、bubblewrap、Claude CLI，并告诉你缺什么、怎么装。Ubuntu 24.04 一般只需要：

```sh
sudo apt install git bubblewrap python3-venv
```

## 2. 自带密钥（BYOK）

```sh
./orch keys dsh            # DeepSeek API key，不回显地输入；没有本地额度上限
./orch keys jev            # 可选：Vercel AI Gateway key（Jev 分诊/预筛）
```

- key 只在本机终端输入，存成 `.data/secrets/<provider>.key`（权限 0600），不进仓库、日志或聊天。
- 花费只受 DeepSeek 账户余额限制；本机只记录花费（`./orch doctor` 显示累计）。
- 无人值守时可以从标准输入读：`./orch keys dsh --key-stdin < ~/deepseek.key`。
  不要把 key 写在命令行参数里。
- 没装 DeepSeek Harness 的 `dsh` CLI 时，DSF 直接调用 DeepSeek 的 API（同一个模型
  `deepseek-flash`、同一把 key、同一份预算账本，每次调用只发一个请求）。装了 `dsh` 时走它。
  可以用 `ORCHESTRA_DSF_TRANSPORT=api` 或 `cli` 指定。
- 每次 DSF 调用先预留 ¥3，按返回的 token 用保守价结算（通常几分钱）。被拒绝的请求
  （401/402/429/400）释放预留。

Claude 只用订阅登录，不用 API key（这是有意的：不切换计费方式）：

```sh
curl -fsSL https://claude.ai/install.sh | bash   # 如果还没装 Claude Code
claude auth login
```

## 3. 体检与演示

```sh
./orch doctor              # Python、沙箱、索引、Claude 登录、DSF 通道/key/余额、Jev
./orch demo ~/orch-demo    # 新建一个演示仓库，跑完整的 plan → run
```

演示大约花 1 次 Claude 规划调用和若干次 DSF 调用（每次几分钱）。报告在
`~/orch-demo/.git/orch/<run>/REPORT.md`，结果在该仓库的 `orch/<run>` 分支。

## 4. 用在自己的项目上

```sh
./orch --repo ~/code/app plan spec.md          # 规划（大项目加 --modules；多份择优加 --samples 3）
./orch --repo ~/code/app run --jobs 8          # 执行；可重复执行 = resume
./orch --repo ~/code/app status                # 状态；超过 30 个任务时显示汇总（--all 逐个列出）
./orch --repo ~/code/app why R3                # 需求 → 测试 → 任务 → commit
./orch --repo ~/code/app trace some_function   # 代码 → 写它的任务
./orch --repo ~/code/app cost --by model       # token、缓存命中、人民币
./orch --repo ~/code/app harden                # 让 Claude 补测试杀死存活变异体
./orch --repo ~/code/app audit                 # 检查一个 run 的全部不变量和测试
```

退出码：0 全部合并，1 环境或输入错误，3 有 PARKED，4 暂停（额度/登录/限流/中断/花费上限），
5 规划未通过 lint。结果只在本地 `orch/<run>` 分支，满意后自己 `git merge`。

想在任何目录直接用 `orch`：`ln -s "$PWD/orch" ~/.local/bin/orch`。

## 常见问题

**`bwrap cannot create user namespaces`（Ubuntu 23.10+/24.04）**：系统限制了非特权用户命名空间。
二选一，需要你自己决定并执行：

```sh
# 只给 bwrap 放行（推荐）
sudo tee /etc/apparmor.d/bwrap >/dev/null <<'PROFILE'
abi <abi/4.0>,
include <tunables/global>
profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
PROFILE
sudo apparmor_parser -r /etc/apparmor.d/bwrap

# 或者全局关闭该限制（影响整台机器）
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
```

**`python venv is missing`**：`sudo apt install python3-venv`，然后重新运行 `./install.sh`。

**`claude ... login: logged_out`**：运行 `claude auth login`（订阅账号）。

**DSF 返回 `auth_required`**：key 无效，重新运行 `./orch keys dsh`。**`quota_exhausted`**：
DeepSeek 账户余额不足（HTTP 402），充值后重试。

**确认 DSF 真的用了 Flash**：`.data/dsh/guard.log` 每行最后是 DeepSeek 返回的模型名。
