###############################################
# docker-updater
# 通过挂载 /var/run/docker.sock 自动更新容器
# 用法:
#   docker run --rm \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     rehiy/docker-updater [容器名...]
#
#   docker run -d --name docker-updater \
#     -v /var/run/docker.sock:/var/run/docker.sock \
#     rehiy/docker-updater watch --interval 3600 --window 02:00-06:00 [容器名...]
###############################################
FROM python:3-alpine

COPY updater.py /usr/local/bin/docker-updater
RUN chmod +x /usr/local/bin/docker-updater

ENTRYPOINT ["docker-updater"]
