# 多阶段构建：运行镜像不含 Maven 与源码。
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /build
COPY pom.xml .
# 先只拷 pom 拉依赖，源码变更时这一层仍能命中缓存。
RUN mvn -q -B dependency:go-offline
COPY src ./src
RUN mvn -q -B -DskipTests package

# 用 jre 而非 jre-alpine：temurin 的 alpine 变体没有 linux/arm64 manifest，
# 在 Apple Silicon 上构建会失败。
FROM eclipse-temurin:17-jre
RUN groupadd -r runbookguard && useradd -r -g runbookguard runbookguard \
    && apt-get update && apt-get install -y --no-install-recommends wget \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /build/target/control-plane-*.jar /app/control-plane.jar
# 非 root 运行（M0 安全模型的 Sandbox 要求）。
USER runbookguard
EXPOSE 8080
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-jar", "/app/control-plane.jar"]
