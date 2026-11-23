-- 种子数据：每个场景对应 README 中的试跑用例

-- ============================================================
-- 设备 1：存在阻断（handover 不可延迟 NO ACTION；work_order 可延迟且留存）
-- ============================================================
INSERT INTO equipment (id, code, name, location) VALUES
    (1, 'EQ-001', '一号泵', 'A 区');
INSERT INTO inspection (id, equipment_id, inspected_at, result) VALUES
    (101, 1, '2026-11-01 09:00:00+00', 'ok');
INSERT INTO retest (id, inspection_id, retested_at, result) VALUES
    (1001, 101, '2026-11-02 09:00:00+00', 'ok');
INSERT INTO handover (id, equipment_id, handed_to, handed_at) VALUES
    (201, 1, '张三', '2026-11-03 10:00:00+00');       -- 阻断：NO ACTION 留存
INSERT INTO handover_line (id, handover_id, note) VALUES
    (301, 201, '随箱附件');
INSERT INTO work_order (id, equipment_id, title) VALUES
    (401, 1, '维修单 #401');                            -- 阻断：可延迟 NO ACTION，事务末检查
INSERT INTO work_order_note (id, work_order_id, body) VALUES
    (501, 401, '已联系厂家');
INSERT INTO service_contract (id, equipment_id, contract_no) VALUES
    (701, 1, 'SC-001');                                 -- 超范围：RESTRICT
INSERT INTO external_ref (id, equipment_code, source) VALUES
    (801, 'EQ-001', 'EAM');                             -- 超范围：引用非主键唯一键
INSERT INTO location_log (id, equipment_id, logged_at) VALUES
    (901, 1, '2026-11-01 08:00:00+00');                 -- 超范围：SET NULL
INSERT INTO tag_assignment (tag_id, equipment_id, assigned_at) VALUES
    (11, 1, '2026-11-01 08:30:00+00');                  -- 超范围：复合主键表

-- ============================================================
-- 设备 2：干净删除（全部走 CASCADE / 可延迟引用行也会删）
-- ============================================================
INSERT INTO equipment (id, code, name, location) VALUES
    (2, 'EQ-002', '二号阀', 'B 区');
INSERT INTO inspection (id, equipment_id, inspected_at, result) VALUES
    (102, 2, '2026-11-05 09:00:00+00', 'ok');
INSERT INTO retest (id, inspection_id, retested_at, result) VALUES
    (1002, 102, '2026-11-06 09:00:00+00', 'ok');
INSERT INTO retest_review (id, retest_id, equipment_id, comment) VALUES
    (601, 1002, 2, '复核通过');                          -- 会随级联删除，不阻断
INSERT INTO inspection_attachment (id, equipment_id, inspection_id, url) VALUES
    (651, 2, 102, 's3://att/651');                      -- 两条级联路径命中同一行

-- ============================================================
-- 设备 3：可延迟 NO ACTION 的引用行留存 -> 事务末阻断
-- ============================================================
INSERT INTO equipment (id, code, name, location) VALUES
    (3, 'EQ-003', '三号表', 'C 区');
INSERT INTO inspection (id, equipment_id, inspected_at, result) VALUES
    (103, 3, '2026-11-07 09:00:00+00', 'warning');
INSERT INTO retest (id, inspection_id, retested_at, result) VALUES
    (1003, 103, '2026-11-08 09:00:00+00', 'ok');
INSERT INTO retest_review (id, retest_id, equipment_id, comment) VALUES
    (602, 1003, NULL, '待人工确认');                     -- 留存 -> 阻断（事务末检查）

-- ============================================================
-- 设备 4 / 5：仅超范围边（复合主键表 + SET NULL）
-- ============================================================
INSERT INTO equipment (id, code, name, location) VALUES
    (4, 'EQ-004', '四号柜', 'D 区'),
    (5, 'EQ-005', '五号机', 'E 区');
INSERT INTO tag_assignment (tag_id, equipment_id, assigned_at) VALUES
    (12, 4, '2026-11-09 08:30:00+00');
INSERT INTO location_log (id, equipment_id, logged_at) VALUES
    (902, 5, '2026-11-09 08:00:00+00');

-- ============================================================
-- 自引用环路（CASCADE 部件树）
-- ============================================================
INSERT INTO part (id, parent_id, name) VALUES
    (1, NULL, '根部件'),
    (2, 1, '子部件'),
    (3, 2, '孙部件');

-- ============================================================
-- 两表互指环路（CASCADE）
-- CASCADE 外键不可 DEFERRABLE，先插 NULL 行再互指建环
-- ============================================================
INSERT INTO node_a (id, b_id) VALUES (1, NULL);
INSERT INTO node_b (id, a_id) VALUES (1, NULL);
UPDATE node_a SET b_id = 1 WHERE id = 1;
UPDATE node_b SET a_id = 1 WHERE id = 1;
