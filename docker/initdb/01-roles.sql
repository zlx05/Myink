-- Myink 阶段 5 compose 首启：建业务应用角色。
-- 背景：手工容器时代 myink_app 角色 + 表权限是手动建的；compose 自动化补齐。
-- myink_app：非超级 + NOBYPASSRLS（受 RLS 约束，§14.1 坑 3：超级用户永远绕过 RLS，
-- 业务必须走受约束角色）。建表由 `myink init` 用超级用户 myink 执行（表 owner=myink），
-- ALTER DEFAULT PRIVILEGES 保证 myink 后续新建表自动授 DML 给 myink_app。
-- CREATE ON SCHEMA public：langgraph checkpointer（§6.7）用应用连接 saver.setup()
-- 建内部表（checkpoints/checkpoint_blobs/checkpoint_writes，§14 隔离清单，非租户表），
-- 应用角色须能在 public 建表（PG15+ public 默认对 PUBLIC 无 CREATE）。
CREATE ROLE myink_app LOGIN PASSWORD 'myink' NOBYPASSRLS;
GRANT CONNECT ON DATABASE myink TO myink_app;
GRANT CREATE ON SCHEMA public TO myink_app;
ALTER DEFAULT PRIVILEGES FOR ROLE myink IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO myink_app;
ALTER DEFAULT PRIVILEGES FOR ROLE myink IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO myink_app;
