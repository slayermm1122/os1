module.exports = {
  apps: [
    {
      name: "os1",
      cwd: __dirname,
      script: ".venv/bin/uvicorn",
      args: ["backend.app:app", "--host", "127.0.0.1", "--port", "8000"],
      interpreter: "none",
      instances: 1,
      exec_mode: "fork",
      autorestart: true,
      watch: false,
      restart_delay: 1000,
      kill_timeout: 10000,
      max_memory_restart: "512M",
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
