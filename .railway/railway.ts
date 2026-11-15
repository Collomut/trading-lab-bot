import { defineRailway, github, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const trialVolume = volume("trial-volume", {
    alerts: { usage: { "100": {}, "80": {}, "95": {} } },
    allowOnlineResize: true,
    region: "europe-west4-drams3a",
    sizeMB: 500,
  });

  const trial = service("trial", {
    source: github("Collomut/trading-lab-bot", {
      branch: "testnet-prelaunch-safeguards",
      checkSuites: true,
      rootDirectory: "/",
    }),
    build: { buildEnvironment: "V3", builder: "RAILPACK" },
    start: "python start.py",
    healthcheck: "/health/liveness",
    healthcheckTimeout: 100,
    replicas: { "europe-west4": 1 },
    deploy: { ipv6EgressEnabled: true, overlapSeconds: 0 },
    volumeMounts: { "/app/data": trialVolume },
  });

  return project("heartfelt-energy", {
    variables: { managed: false },
    resources: [trial, trialVolume],
  });
});

