-- 样例库：设备运维（巡检 / 复测 / 交接单）删除预检
-- 覆盖：CASCADE 级联、可延迟 NO ACTION、阻断引用、环路、多路径去重、
--       复合主键表、指向非主键唯一键的外键、RESTRICT（超出支持范围）

CREATE TABLE equipment (
    id          BIGINT PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    location    TEXT
);

-- 巡检记录：设备删除 -> CASCADE 带走
CREATE TABLE inspection (
    id          BIGINT PRIMARY KEY,
    equipment_id BIGINT NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    inspected_at TIMESTAMPTZ NOT NULL,
    result      TEXT
);

-- 复测记录：巡检删除 -> CASCADE 带走（多级级联）
CREATE TABLE retest (
    id           BIGINT PRIMARY KEY,
    inspection_id BIGINT NOT NULL REFERENCES inspection(id) ON DELETE CASCADE,
    retested_at  TIMESTAMPTZ NOT NULL,
    result       TEXT
);

-- 交接单：普通 NO ACTION，不可延迟 —— 存在引用即阻断删除（事务末检查同样报错）
CREATE TABLE handover (
    id           BIGINT PRIMARY KEY,
    equipment_id BIGINT NOT NULL REFERENCES equipment(id) ON DELETE NO ACTION,
    handed_to    TEXT NOT NULL,
    handed_at    TIMESTAMPTZ NOT NULL
);

-- 交接明细：可延迟 NO ACTION（INITIALLY DEFERRED），事务末才检查
CREATE TABLE handover_line (
    id           BIGINT PRIMARY KEY,
    handover_id  BIGINT NOT NULL
        REFERENCES handover(id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    note         TEXT
);

-- 工单：可延迟 NO ACTION，若工单行本身也会被删（同事务级联命中）则不阻断
CREATE TABLE work_order (
    id           BIGINT PRIMARY KEY,
    equipment_id BIGINT NOT NULL
        REFERENCES equipment(id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    title        TEXT NOT NULL
);

-- 工单备注：CASCADE，构成 equipment -> work_order 的第二条删除路径
CREATE TABLE work_order_note (
    id            BIGINT PRIMARY KEY,
    work_order_id BIGINT NOT NULL REFERENCES work_order(id) ON DELETE CASCADE,
    body          TEXT
);

-- 自引用环路：部件树，CASCADE 自引用
CREATE TABLE part (
    id        BIGINT PRIMARY KEY,
    parent_id BIGINT REFERENCES part(id) ON DELETE CASCADE,
    name      TEXT NOT NULL
);

-- 两表互指环路：均为 CASCADE
CREATE TABLE node_a (
    id     BIGINT PRIMARY KEY,
    b_id   BIGINT
);
CREATE TABLE node_b (
    id     BIGINT PRIMARY KEY,
    a_id   BIGINT
);
ALTER TABLE node_a ADD CONSTRAINT node_a_b_fk
    FOREIGN KEY (b_id) REFERENCES node_b(id) ON DELETE CASCADE;
ALTER TABLE node_b ADD CONSTRAINT node_b_a_fk
    FOREIGN KEY (a_id) REFERENCES node_a(id) ON DELETE CASCADE;

-- 巡检附件：同时以 CASCADE 指向 equipment 和 inspection，
-- 从同一设备出发存在两条级联路径命中同一行 —— 回执只列一次
CREATE TABLE inspection_attachment (
    id            BIGINT PRIMARY KEY,
    equipment_id  BIGINT NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    inspection_id BIGINT NOT NULL REFERENCES inspection(id) ON DELETE CASCADE,
    url           TEXT NOT NULL
);

-- 复测复核：对 retest 是可延迟 NO ACTION；若本行经 equipment 级联一起删除则不构成阻断，
-- 若本行留存（equipment_id 为空）则在事务末检查时阻断
CREATE TABLE retest_review (
    id           BIGINT PRIMARY KEY,
    retest_id    BIGINT NOT NULL
        REFERENCES retest(id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    equipment_id BIGINT REFERENCES equipment(id) ON DELETE CASCADE,
    comment      TEXT
);

-- 设备外部标识：指向 equipment.code（非主键唯一键）—— 超出支持范围
CREATE TABLE external_ref (
    id          BIGINT PRIMARY KEY,
    equipment_code TEXT NOT NULL REFERENCES equipment(code) ON DELETE CASCADE,
    source      TEXT
);

-- 复合主键表（单列主键前提不满足）—— 超出支持范围
CREATE TABLE tag_assignment (
    tag_id      BIGINT NOT NULL,
    equipment_id BIGINT NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
    assigned_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tag_id, equipment_id)
);

-- RESTRICT 动作 —— 超出支持范围（本服务只分析 CASCADE / NO ACTION）
CREATE TABLE service_contract (
    id           BIGINT PRIMARY KEY,
    equipment_id BIGINT NOT NULL REFERENCES equipment(id) ON DELETE RESTRICT,
    contract_no  TEXT NOT NULL
);

-- SET NULL 动作 —— 超出支持范围
CREATE TABLE location_log (
    id           BIGINT PRIMARY KEY,
    equipment_id BIGINT REFERENCES equipment(id) ON DELETE SET NULL,
    logged_at    TIMESTAMPTZ NOT NULL
);
