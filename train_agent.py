"""
train_agent.py
==============
Training script for the InternHub Vanna agent.
Expects that the environment variables (GROQ, DB_PASSWORD, CHROMA_PATH) are set.
"""

import logging
from vanna_setup import vn, connect_to_postgres

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── DDL (Data Definition Language) ─────────────────────────────────────
# Mirrors the real schema in intern-management-system/sql/schema.sql

DDL_STATEMENTS = [
    # Enums
    """
    CREATE TYPE user_role AS ENUM ('admin', 'mentor', 'intern');
    """,
    """
    CREATE TYPE task_status AS ENUM ('pending', 'in-progress', 'completed');
    """,
    """
    CREATE TYPE task_priority AS ENUM ('low', 'medium', 'high');
    """,
    """
    CREATE TYPE intern_status AS ENUM ('active', 'inactive');
    """,

    # Core users table
    """
    CREATE TABLE users (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role user_role NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,

    # User profiles
    """
    CREATE TABLE profiles (
        user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        department TEXT,
        phone TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,

    # Intern-specific data
    """
    CREATE TABLE interns (
        user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        mentor_id UUID REFERENCES users(id) ON DELETE SET NULL,
        admin_id UUID REFERENCES users(id) ON DELETE SET NULL,
        college_name TEXT,
        university TEXT,
        start_date DATE,
        end_date DATE,
        status intern_status NOT NULL DEFAULT 'active',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT interns_date_check
            CHECK (end_date IS NULL OR start_date IS NULL OR end_date >= start_date)
    );
    """,

    # Tasks
    """
    CREATE TABLE tasks (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        title TEXT NOT NULL,
        description TEXT,
        assigned_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        assigned_to_all BOOLEAN NOT NULL DEFAULT FALSE,
        deadline DATE,
        priority task_priority NOT NULL DEFAULT 'medium',
        status task_status NOT NULL DEFAULT 'pending',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,

    # Many-to-many task assignments
    """
    CREATE TABLE task_assignments (
        task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        intern_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (task_id, intern_id)
    );
    """,

    # Intern daily/weekly reports
    """
    CREATE TABLE reports (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        intern_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        report_date DATE NOT NULL,
        work_description TEXT NOT NULL,
        hours_worked NUMERIC(4,2) NOT NULL CHECK (hours_worked >= 0 AND hours_worked <= 24),
        mentor_feedback TEXT,
        submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,

    # Password reset tokens
    """
    CREATE TABLE password_reset_tokens (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        email TEXT NOT NULL,
        token TEXT,
        otp_code TEXT,
        type TEXT NOT NULL CHECK (type IN ('otp', 'reset')),
        expires_at TIMESTAMPTZ NOT NULL,
        attempts INTEGER DEFAULT 0,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        used_at TIMESTAMPTZ
    );
    """,
]

# ── Documentation ──────────────────────────────────────────────────────

DOCUMENTATION = [
    # Roles
    """
    The system supports three user roles stored in the users.role column:
    'admin' – can manage all interns, mentors, and tasks.
    'mentor' – can view and manage assigned interns and tasks.
    'intern' – can view own tasks and submit reports.
    """,

    # Active intern definition
    """
    An 'active intern' is one whose interns.status = 'active'
    and their end_date is NULL or in the future:
    WHERE i.status = 'active' AND (i.end_date IS NULL OR i.end_date >= CURRENT_DATE)
    """,

    # Users ↔ Profiles relationship
    """
    Every user has a one-to-one profile in the 'profiles' table.
    To get a user's display name, department, or phone, JOIN users with profiles
    on users.id = profiles.user_id.
    """,

    # Intern ↔ Mentor relationship
    """
    Each intern record in the 'interns' table has a mentor_id and admin_id
    that reference users.id. To find a mentor's name you must join:
    interns.mentor_id = users.id then users.id = profiles.user_id.
    """,

    # Tasks relationships
    """
    Tasks are created by admins or mentors (tasks.assigned_by).
    They can be assigned to specific interns via the task_assignments junction table,
    or to ALL interns when tasks.assigned_to_all = TRUE.
    Task statuses are: 'pending', 'in-progress', 'completed'.
    Task priorities are: 'low', 'medium', 'high'.
    """,

    # Reports
    """
    Interns submit daily/weekly reports via the 'reports' table.
    Each report has: report_date, work_description, hours_worked (0-24),
    and an optional mentor_feedback field filled by the mentor.
    """,

    # Date conventions
    """
    All timestamps use TIMESTAMPTZ (with time zone). Date-only columns
    (start_date, end_date, deadline, report_date) use the DATE type.
    When filtering by month use:
      date_column >= 'YYYY-MM-01' AND date_column < 'YYYY-MM-01'::date + INTERVAL '1 month'
    """,
]

# ── Golden Queries (Question → SQL pairs) ──────────────────────────────

GOLDEN_QUERIES = [
    # ────────────────────── USER / PROFILE QUERIES ──────────────────────
    {
        "question": "List all users with their profiles.",
        "sql": """
            SELECT u.id, u.email, u.role, p.name, p.department, p.phone
            FROM users u
            JOIN profiles p ON u.id = p.user_id
            ORDER BY p.name;
        """
    },
    {
        "question": "How many users are there for each role?",
        "sql": """
            SELECT role, COUNT(*) AS user_count
            FROM users
            GROUP BY role
            ORDER BY user_count DESC;
        """
    },
    {
        "question": "Show all admins with their names.",
        "sql": """
            SELECT u.id, u.email, p.name
            FROM users u
            JOIN profiles p ON u.id = p.user_id
            WHERE u.role = 'admin'
            ORDER BY p.name;
        """
    },
    {
        "question": "Show all mentors with their department.",
        "sql": """
            SELECT u.id, u.email, p.name, p.department
            FROM users u
            JOIN profiles p ON u.id = p.user_id
            WHERE u.role = 'mentor'
            ORDER BY p.name;
        """
    },
    {
        "question": "Find users who registered in the last 7 days.",
        "sql": """
            SELECT u.id, u.email, u.role, p.name, u.created_at
            FROM users u
            JOIN profiles p ON u.id = p.user_id
            WHERE u.created_at >= NOW() - INTERVAL '7 days'
            ORDER BY u.created_at DESC;
        """
    },
    {
        "question": "How many users are in each department?",
        "sql": """
            SELECT p.department, COUNT(*) AS total
            FROM profiles p
            WHERE p.department IS NOT NULL
            GROUP BY p.department
            ORDER BY total DESC;
        """
    },

    # ────────────────────── INTERN QUERIES ──────────────────────────────
    {
        "question": "Show all active interns with their name and college.",
        "sql": """
            SELECT p.name, i.college_name, i.university, i.start_date, i.end_date
            FROM interns i
            JOIN profiles p ON i.user_id = p.user_id
            WHERE i.status = 'active'
              AND (i.end_date IS NULL OR i.end_date >= CURRENT_DATE)
            ORDER BY p.name;
        """
    },
    {
        "question": "How many active interns are there?",
        "sql": """
            SELECT COUNT(*) AS active_intern_count
            FROM interns
            WHERE status = 'active'
              AND (end_date IS NULL OR end_date >= CURRENT_DATE);
        """
    },
    {
        "question": "How many inactive interns are there?",
        "sql": """
            SELECT COUNT(*) AS inactive_intern_count
            FROM interns
            WHERE status = 'inactive';
        """
    },
    {
        "question": "List all interns assigned to a specific mentor.",
        "sql": """
            SELECT p_intern.name AS intern_name, p_mentor.name AS mentor_name,
                   i.college_name, i.start_date, i.end_date
            FROM interns i
            JOIN profiles p_intern ON i.user_id = p_intern.user_id
            JOIN profiles p_mentor ON i.mentor_id = p_mentor.user_id
            ORDER BY p_mentor.name, p_intern.name;
        """
    },
    {
        "question": "Which mentor has the most interns?",
        "sql": """
            SELECT p.name AS mentor_name, COUNT(i.user_id) AS intern_count
            FROM interns i
            JOIN profiles p ON i.mentor_id = p.user_id
            WHERE i.status = 'active'
            GROUP BY p.name
            ORDER BY intern_count DESC
            LIMIT 1;
        """
    },
    {
        "question": "List interns whose internship ends this month.",
        "sql": """
            SELECT p.name, i.end_date, i.college_name
            FROM interns i
            JOIN profiles p ON i.user_id = p.user_id
            WHERE i.end_date >= date_trunc('month', CURRENT_DATE)
              AND i.end_date < date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
            ORDER BY i.end_date;
        """
    },
    {
        "question": "List interns who started in the last 30 days.",
        "sql": """
            SELECT p.name, i.start_date, i.college_name, i.university
            FROM interns i
            JOIN profiles p ON i.user_id = p.user_id
            WHERE i.start_date >= CURRENT_DATE - INTERVAL '30 days'
            ORDER BY i.start_date DESC;
        """
    },
    {
        "question": "Show interns without a mentor assigned.",
        "sql": """
            SELECT p.name, u.email, i.college_name
            FROM interns i
            JOIN users u ON i.user_id = u.id
            JOIN profiles p ON i.user_id = p.user_id
            WHERE i.mentor_id IS NULL
            ORDER BY p.name;
        """
    },
    {
        "question": "List all interns from a specific university.",
        "sql": """
            SELECT p.name, i.university, i.college_name, i.start_date, i.status
            FROM interns i
            JOIN profiles p ON i.user_id = p.user_id
            WHERE i.university ILIKE '%university_name%'
            ORDER BY p.name;
        """
    },
    {
        "question": "How many interns are there per university?",
        "sql": """
            SELECT i.university, COUNT(*) AS intern_count
            FROM interns i
            WHERE i.university IS NOT NULL
            GROUP BY i.university
            ORDER BY intern_count DESC;
        """
    },

    # ────────────────────── TASK QUERIES ────────────────────────────────
    {
        "question": "Show all pending tasks.",
        "sql": """
            SELECT t.id, t.title, t.priority, t.deadline, p.name AS assigned_by_name
            FROM tasks t
            JOIN profiles p ON t.assigned_by = p.user_id
            WHERE t.status = 'pending'
            ORDER BY t.deadline ASC NULLS LAST;
        """
    },
    {
        "question": "Show all high priority tasks.",
        "sql": """
            SELECT t.id, t.title, t.description, t.deadline, t.status,
                   p.name AS assigned_by_name
            FROM tasks t
            JOIN profiles p ON t.assigned_by = p.user_id
            WHERE t.priority = 'high'
            ORDER BY t.deadline ASC NULLS LAST;
        """
    },
    {
        "question": "How many tasks are there by status?",
        "sql": """
            SELECT status, COUNT(*) AS task_count
            FROM tasks
            GROUP BY status
            ORDER BY task_count DESC;
        """
    },
    {
        "question": "How many tasks are there by priority?",
        "sql": """
            SELECT priority, COUNT(*) AS task_count
            FROM tasks
            GROUP BY priority
            ORDER BY task_count DESC;
        """
    },
    {
        "question": "Show all tasks assigned to a specific intern.",
        "sql": """
            SELECT t.title, t.description, t.priority, t.status, t.deadline
            FROM tasks t
            JOIN task_assignments ta ON t.id = ta.task_id
            JOIN profiles p ON ta.intern_id = p.user_id
            WHERE p.name ILIKE '%intern_name%'
            ORDER BY t.deadline ASC NULLS LAST;
        """
    },
    {
        "question": "Which intern has the most tasks assigned?",
        "sql": """
            SELECT p.name, COUNT(ta.task_id) AS task_count
            FROM task_assignments ta
            JOIN profiles p ON ta.intern_id = p.user_id
            GROUP BY p.name
            ORDER BY task_count DESC
            LIMIT 1;
        """
    },
    {
        "question": "List overdue tasks that are not completed.",
        "sql": """
            SELECT t.title, t.priority, t.deadline, t.status,
                   p.name AS assigned_by_name
            FROM tasks t
            JOIN profiles p ON t.assigned_by = p.user_id
            WHERE t.deadline < CURRENT_DATE
              AND t.status != 'completed'
            ORDER BY t.deadline ASC;
        """
    },
    {
        "question": "Show tasks created in the last 7 days.",
        "sql": """
            SELECT t.title, t.priority, t.status, t.deadline, t.created_at,
                   p.name AS created_by
            FROM tasks t
            JOIN profiles p ON t.assigned_by = p.user_id
            WHERE t.created_at >= NOW() - INTERVAL '7 days'
            ORDER BY t.created_at DESC;
        """
    },
    {
        "question": "Show tasks assigned to all interns.",
        "sql": """
            SELECT t.id, t.title, t.priority, t.status, t.deadline
            FROM tasks t
            WHERE t.assigned_to_all = TRUE
            ORDER BY t.created_at DESC;
        """
    },
    {
        "question": "How many tasks has each mentor or admin created?",
        "sql": """
            SELECT p.name, u.role, COUNT(t.id) AS tasks_created
            FROM tasks t
            JOIN users u ON t.assigned_by = u.id
            JOIN profiles p ON u.id = p.user_id
            GROUP BY p.name, u.role
            ORDER BY tasks_created DESC;
        """
    },

    # ────────────────────── REPORT QUERIES ──────────────────────────────
    {
        "question": "Show all reports submitted today.",
        "sql": """
            SELECT p.name, r.work_description, r.hours_worked, r.submitted_at
            FROM reports r
            JOIN profiles p ON r.intern_id = p.user_id
            WHERE r.report_date = CURRENT_DATE
            ORDER BY r.submitted_at DESC;
        """
    },
    {
        "question": "How many total hours has each intern worked?",
        "sql": """
            SELECT p.name, SUM(r.hours_worked) AS total_hours
            FROM reports r
            JOIN profiles p ON r.intern_id = p.user_id
            GROUP BY p.name
            ORDER BY total_hours DESC;
        """
    },
    {
        "question": "Show the average hours worked per intern this month.",
        "sql": """
            SELECT p.name, ROUND(AVG(r.hours_worked), 2) AS avg_hours
            FROM reports r
            JOIN profiles p ON r.intern_id = p.user_id
            WHERE r.report_date >= date_trunc('month', CURRENT_DATE)
              AND r.report_date < date_trunc('month', CURRENT_DATE) + INTERVAL '1 month'
            GROUP BY p.name
            ORDER BY avg_hours DESC;
        """
    },
    {
        "question": "Show reports that have not received mentor feedback.",
        "sql": """
            SELECT p.name, r.report_date, r.work_description, r.hours_worked
            FROM reports r
            JOIN profiles p ON r.intern_id = p.user_id
            WHERE r.mentor_feedback IS NULL
            ORDER BY r.report_date DESC;
        """
    },
    {
        "question": "Show the total reports submitted per intern.",
        "sql": """
            SELECT p.name, COUNT(r.id) AS report_count
            FROM reports r
            JOIN profiles p ON r.intern_id = p.user_id
            GROUP BY p.name
            ORDER BY report_count DESC;
        """
    },
    {
        "question": "Which intern worked the most hours last week?",
        "sql": """
            SELECT p.name, SUM(r.hours_worked) AS weekly_hours
            FROM reports r
            JOIN profiles p ON r.intern_id = p.user_id
            WHERE r.report_date >= CURRENT_DATE - INTERVAL '7 days'
            GROUP BY p.name
            ORDER BY weekly_hours DESC
            LIMIT 1;
        """
    },
    {
        "question": "Show daily report summary for a specific date.",
        "sql": """
            SELECT p.name, r.work_description, r.hours_worked, r.mentor_feedback
            FROM reports r
            JOIN profiles p ON r.intern_id = p.user_id
            WHERE r.report_date = CURRENT_DATE
            ORDER BY p.name;
        """
    },

    # ────────────────────── CROSS-TABLE / DASHBOARD QUERIES ─────────────
    {
        "question": "Give me a dashboard overview of the system.",
        "sql": """
            SELECT
                (SELECT COUNT(*) FROM users) AS total_users,
                (SELECT COUNT(*) FROM users WHERE role = 'intern') AS total_interns,
                (SELECT COUNT(*) FROM users WHERE role = 'mentor') AS total_mentors,
                (SELECT COUNT(*) FROM users WHERE role = 'admin') AS total_admins,
                (SELECT COUNT(*) FROM interns WHERE status = 'active') AS active_interns,
                (SELECT COUNT(*) FROM tasks) AS total_tasks,
                (SELECT COUNT(*) FROM tasks WHERE status = 'pending') AS pending_tasks,
                (SELECT COUNT(*) FROM tasks WHERE status = 'completed') AS completed_tasks,
                (SELECT COUNT(*) FROM reports) AS total_reports;
        """
    },
    {
        "question": "Show each intern with their mentor name, task count, and total hours worked.",
        "sql": """
            SELECT
                p_intern.name AS intern_name,
                p_mentor.name AS mentor_name,
                COALESCE(task_counts.task_count, 0) AS tasks_assigned,
                COALESCE(report_totals.total_hours, 0) AS total_hours_worked
            FROM interns i
            JOIN profiles p_intern ON i.user_id = p_intern.user_id
            LEFT JOIN profiles p_mentor ON i.mentor_id = p_mentor.user_id
            LEFT JOIN (
                SELECT ta.intern_id, COUNT(*) AS task_count
                FROM task_assignments ta
                GROUP BY ta.intern_id
            ) task_counts ON i.user_id = task_counts.intern_id
            LEFT JOIN (
                SELECT r.intern_id, SUM(r.hours_worked) AS total_hours
                FROM reports r
                GROUP BY r.intern_id
            ) report_totals ON i.user_id = report_totals.intern_id
            WHERE i.status = 'active'
            ORDER BY p_intern.name;
        """
    },
    {
        "question": "Show the completion rate of tasks per intern.",
        "sql": """
            SELECT
                p.name,
                COUNT(ta.task_id) AS total_tasks,
                COUNT(CASE WHEN t.status = 'completed' THEN 1 END) AS completed,
                ROUND(
                    100.0 * COUNT(CASE WHEN t.status = 'completed' THEN 1 END)
                    / NULLIF(COUNT(ta.task_id), 0), 1
                ) AS completion_rate_pct
            FROM task_assignments ta
            JOIN tasks t ON ta.task_id = t.id
            JOIN profiles p ON ta.intern_id = p.user_id
            GROUP BY p.name
            ORDER BY completion_rate_pct DESC;
        """
    },
    {
        "question": "List interns who have not submitted any report this week.",
        "sql": """
            SELECT p.name, u.email
            FROM interns i
            JOIN users u ON i.user_id = u.id
            JOIN profiles p ON i.user_id = p.user_id
            WHERE i.status = 'active'
              AND i.user_id NOT IN (
                  SELECT r.intern_id
                  FROM reports r
                  WHERE r.report_date >= CURRENT_DATE - INTERVAL '7 days'
              )
            ORDER BY p.name;
        """
    },
    {
        "question": "Show the top 5 interns by total hours worked.",
        "sql": """
            SELECT p.name, SUM(r.hours_worked) AS total_hours
            FROM reports r
            JOIN profiles p ON r.intern_id = p.user_id
            GROUP BY p.name
            ORDER BY total_hours DESC
            LIMIT 5;
        """
    },
    {
        "question": "Show departments with the number of interns in each.",
        "sql": """
            SELECT p.department, COUNT(i.user_id) AS intern_count
            FROM interns i
            JOIN profiles p ON i.user_id = p.user_id
            WHERE p.department IS NOT NULL
            GROUP BY p.department
            ORDER BY intern_count DESC;
        """
    },
    {
        "question": "Find interns with no tasks assigned.",
        "sql": """
            SELECT p.name, u.email
            FROM interns i
            JOIN users u ON i.user_id = u.id
            JOIN profiles p ON i.user_id = p.user_id
            WHERE i.status = 'active'
              AND i.user_id NOT IN (
                  SELECT ta.intern_id FROM task_assignments ta
              )
            ORDER BY p.name;
        """
    },
    {
        "question": "Show the number of tasks created per month.",
        "sql": """
            SELECT
                TO_CHAR(created_at, 'YYYY-MM') AS month,
                COUNT(*) AS tasks_created
            FROM tasks
            GROUP BY TO_CHAR(created_at, 'YYYY-MM')
            ORDER BY month DESC;
        """
    },
    {
        "question": "Show tasks along with how many interns each task is assigned to.",
        "sql": """
            SELECT t.title, t.priority, t.status, t.deadline,
                   COUNT(ta.intern_id) AS assigned_intern_count
            FROM tasks t
            LEFT JOIN task_assignments ta ON t.id = ta.task_id
            GROUP BY t.id, t.title, t.priority, t.status, t.deadline
            ORDER BY assigned_intern_count DESC;
        """
    },
]


def run_training():
    logger.info("Starting training of InternHub AI...")

    # 1. DDL
    for ddl in DDL_STATEMENTS:
        vn.train(ddl=ddl)
    logger.info(f"Trained {len(DDL_STATEMENTS)} DDL statements.")

    # 2. Docs
    for doc in DOCUMENTATION:
        vn.train(documentation=doc)
    logger.info(f"Trained {len(DOCUMENTATION)} documentation blocks.")

    # 3. Golden Queries
    for query in GOLDEN_QUERIES:
        vn.train(question=query['question'], sql=query['sql'])
    logger.info(f"Trained {len(GOLDEN_QUERIES)} golden queries.")

    logger.info("Training complete! ChromaDB seed successful.")


if __name__ == "__main__":
    run_training()
