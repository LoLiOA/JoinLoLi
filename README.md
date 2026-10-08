# JoinLoLi
LoLi Play~~~

In Issues Commit LoLi, ko re de, join da ki ru~

## 提交 Issue 就加入组织

在本仓库**新建 / 重新打开**任意 Issue, workflow 会自动把 **Issue 作者**邀请进
**LoLiOA** (模板里的用户名只用于核对身份):

1. 用**你自己的** GitHub 账号 New Issue -> 选 "申请加入 LoLi" 模板 -> 填用户名 -> 提交。
2. 等几十秒, 机器人会在 Issue 里回复结果。
3. 到你的注册邮箱, 或 [邀请页面](https://github.com/orgs/LoLiOA/invitation)
   点 **Accept** 完成加入。

想手动指定用户, 或只想演练不真正邀请, 跑到
Actions -> **Join LoLi** -> **Run workflow** (可填 `login` / `dry_run`)。

## 一次性配置

邀请成员属于**组织**权限, `github.token` 拿不到, 所以需要额外准备一个令牌:

1. 用一个 **LoLiOA 组织管理员**账号创建 PAT (Fine-grained 或经典均可),
   勾选 `admin:org` -> `Write` 权限 (经典 PAT 勾 `write:org`, `read:org`)。
2. 在仓库 Settings -> Secrets and variables -> Actions 新建 secret
   `ORG_INVITE_TOKEN` = 上面这个 PAT。
3. (可选) 新建 repository variable `LOLI_ORG` 覆盖默认组织名 `LoLiOA`。

> - 组织若开启了 "new member approval", 邀请会先进入待审批状态, 机器人不报错,
>   但需要 owner 批准后才真正生效。
> - **安全**: 邀请对象永远是 Issue 作者本人。模板里填的用户名若与作者不一致,
>   机器人会拒绝并说明 (防止有人替别人刷组织邀请)。
> - PAT 只放在仓库 secret 里, 只授予 admin:org; workflow 只在 issues 事件
>   上运行**主分支**的脚本, 别人提 PR 改脚本不会拿到令牌。

## 本地测试

```bash
python3 -m unittest discover -s tests   # 用假 GitHub API 跑, 不需要联网
GITHUB_TOKEN=fake LOLI_LOGIN=your-name DRY_RUN=true python3 .github/scripts/join_loli.py
```

脚本只依赖 Python 标准库, 逻辑都在
[.github/scripts/join_loli.py](.github/scripts/join_loli.py)。

## MenBa

- [HengXin666](https://github.com/HengXin666)
