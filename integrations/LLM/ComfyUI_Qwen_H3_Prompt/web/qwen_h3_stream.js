import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_CLASS = "QwenH3PromptLocal";
const EVENT_NAME = "qwen_h3_stream";
const LIVE_WIDGET_NAME = "qwen_h3_live_output";

function findNode(nodeId) {
    const graph = app.graph;
    if (!graph) return null;

    const direct = graph.getNodeById?.(nodeId);
    if (direct) return direct;

    const numericId = Number(nodeId);
    if (Number.isFinite(numericId)) {
        const numeric = graph.getNodeById?.(numericId);
        if (numeric) return numeric;
    }

    return graph._nodes?.find((node) => String(node.id) === String(nodeId)) ?? null;
}

function createLivePanel(node) {
    if (node.__qwenH3LivePanel) return node.__qwenH3LivePanel;

    const root = document.createElement("div");
    root.style.boxSizing = "border-box";
    root.style.width = "100%";
    root.style.height = "100%";
    root.style.minHeight = "180px";
    root.style.display = "flex";
    root.style.flexDirection = "column";
    root.style.gap = "6px";
    root.style.padding = "8px";
    root.style.border = "1px solid rgba(255,255,255,0.12)";
    root.style.borderRadius = "6px";
    root.style.background = "rgba(0,0,0,0.22)";
    root.style.overflow = "hidden";

    const status = document.createElement("div");
    status.textContent = "Live output — idle";
    status.style.fontSize = "12px";
    status.style.fontWeight = "600";
    status.style.opacity = "0.85";
    status.style.flex = "0 0 auto";

    const output = document.createElement("textarea");
    output.readOnly = true;
    output.spellcheck = false;
    output.placeholder = "Qwen output will appear here while it is generated...";
    output.style.boxSizing = "border-box";
    output.style.width = "100%";
    output.style.flex = "1 1 auto";
    output.style.minHeight = "145px";
    output.style.resize = "none";
    output.style.padding = "8px";
    output.style.border = "1px solid rgba(255,255,255,0.10)";
    output.style.borderRadius = "4px";
    output.style.background = "rgba(0,0,0,0.30)";
    output.style.color = "inherit";
    output.style.fontFamily = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
    output.style.fontSize = "11px";
    output.style.lineHeight = "1.4";
    output.style.whiteSpace = "pre-wrap";

    root.append(status, output);

    const widget = node.addDOMWidget(
        LIVE_WIDGET_NAME,
        "qwen_h3_live_output",
        root,
        {
            serialize: false,
            hideOnZoom: false,
            getMinHeight: () => 190,
            getHeight: () => 220,
            getValue: () => output.value,
            setValue: (value) => {
                output.value = typeof value === "string" ? value : "";
            },
        },
    );

    const panel = { root, status, output, widget, text: "", stage: "idle" };
    node.__qwenH3LivePanel = panel;

    const width = Math.max(node.size?.[0] ?? 400, 440);
    const height = Math.max(node.size?.[1] ?? 432, 650);
    node.setSize?.([width, height]);

    return panel;
}

function setStatus(panel, text) {
    panel.status.textContent = text;
}

function updatePanel(node, data) {
    const panel = createLivePanel(node);
    const type = data.type ?? "delta";
    const stage = data.stage ?? panel.stage ?? "inference";

    if (type === "reset") {
        panel.text = "";
        panel.output.value = "";
        panel.stage = stage;
        setStatus(panel, "Live output — preparing...");
    } else if (type === "start") {
        panel.stage = stage;
        if (data.reset_text) {
            panel.text = "";
            panel.output.value = "";
        }
        setStatus(
            panel,
            stage === "repair"
                ? "Live output — repairing..."
                : "Live output — generating...",
        );
    } else if (type === "delta") {
        panel.stage = stage;
        const delta = typeof data.delta === "string" ? data.delta : "";
        if (delta) {
            panel.text += delta;
            panel.output.value = panel.text;
            panel.output.scrollTop = panel.output.scrollHeight;
        }
        setStatus(
            panel,
            stage === "repair"
                ? "Live output — repairing..."
                : "Live output — generating...",
        );
    } else if (type === "complete") {
        panel.stage = "complete";
        if (typeof data.text === "string" && data.text.length) {
            panel.text = data.text;
            panel.output.value = data.text;
        }
        const elapsed = Number(data.elapsed);
        const suffix = Number.isFinite(elapsed) ? ` (${elapsed.toFixed(1)}s)` : "";
        setStatus(panel, `Live output — complete${suffix}`);
        panel.output.scrollTop = panel.output.scrollHeight;
    } else if (type === "error") {
        panel.stage = "error";
        const message = typeof data.message === "string" ? data.message : "Unknown error";
        setStatus(panel, `Live output — error: ${message}`);
    }

    node.setDirtyCanvas?.(true, true);
}

api.addEventListener(EVENT_NAME, (event) => {
    const data = event?.detail;
    if (!data || data.node_id === undefined || data.node_id === null) return;

    const node = findNode(data.node_id);
    if (!node || node.comfyClass !== NODE_CLASS) return;

    updatePanel(node, data);
});

app.registerExtension({
    name: "edx.qwen_h3.live_output",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_CLASS) return;

        const originalOnNodeCreated = nodeType.prototype.onNodeCreated;

        nodeType.prototype.onNodeCreated = function () {
            const result = originalOnNodeCreated?.apply(this, arguments);
            createLivePanel(this);
            return result;
        };
    },
});
