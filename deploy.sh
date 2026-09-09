#!/bin/bash
set -e

REMOTE_HOST="10.10.14.104"
REMOTE_USER="kishan"
REMOTE_PATH="/data/admin/lab_exam"

echo "=== Deploying Lab Exam Submission Portal to ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PATH} ==="

# 1. Ensure remote directory exists
ssh ${REMOTE_USER}@${REMOTE_HOST} "sudo mkdir -p ${REMOTE_PATH} && sudo chown -R ${REMOTE_USER}:${REMOTE_USER} ${REMOTE_PATH}"

# 2. Sync portal files
rsync -avz --exclude '.git' --exclude '__pycache__' --exclude 'test_app.py' ./ ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PATH}/

# 3. Install / update systemd service
ssh ${REMOTE_USER}@${REMOTE_HOST} "sudo cp ${REMOTE_PATH}/lab-exam.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable lab-exam.service && sudo systemctl restart lab-exam.service"

# 4. Check service status
ssh ${REMOTE_USER}@${REMOTE_HOST} "systemctl status lab-exam.service --no-pager"

echo "=== Deployment Completed Successfully! ==="
echo "Access URLs:"
echo "1. http://${REMOTE_HOST}/lab_exam/"
echo "2. http://${REMOTE_HOST}:8090/"
echo "Instructor Dashboard: http://${REMOTE_HOST}/lab_exam/admin"
