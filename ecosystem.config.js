module.exports = {
  apps: [{
    name: "mxwbot",
    script: "-m",
    args: "mxwbot.cli.main serve",
    interpreter: "F:\\mxwclaw\\.venv\\Scripts\\python.exe",
    cwd: "F:\\mxwclaw",
    env: {
      PYTHONUNBUFFERED: "1"
    },
    // 日志
    log_date_format: "YYYY-MM-DD HH:mm:ss",
    error_file: "F:\\mxwclaw\\logs\\error.log",
    out_file: "F:\\mxwclaw\\logs\\out.log",
    merge_logs: true,
    // 自动重启
    autorestart: true,
    max_restarts: 10,
    restart_delay: 5000,
    // 崩溃保护：10s 内挂掉不算"正常运行过"
    min_uptime: "10s",
    max_memory_restart: "500M"
  }]
};
