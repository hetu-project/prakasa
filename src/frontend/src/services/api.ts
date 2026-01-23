import { createHttpStreamFactory } from './http-stream';

// Auto-detect base path: if accessing /qwen3/, use /qwen3 as prefix
const getBasePath = (): string => {
  const pathname = window.location.pathname;
  // Match /qwen3/, /qwen2.5/, /deepseek/, /llama-3.1/, /llama-3.3/, etc.
  const match = pathname.match(/^\/(qwen3|qwen2\.5|deepseek|llama-3\.1|llama-3\.3)/);
  return match ? `/${match[1]}` : '';
};

export const API_BASE_URL = import.meta.env.DEV ? '/proxy-api' : getBasePath();

export const getModelList = async (): Promise<readonly any[]> => {
  const response = await fetch(`${API_BASE_URL}/model/list`, { method: 'GET' });
  const message = await response.json();
  if (message.type !== 'model_list') {
    throw new Error(`Invalid message type: ${message.type}.`);
  }
  return message.data;
};

export const initScheduler = async (params: {
  model_name: string;
  init_nodes_num: number;
  is_local_network: boolean;
}): Promise<void> => {
  const response = await fetch(`${API_BASE_URL}/scheduler/init`, {
    method: 'POST',
    body: JSON.stringify(params),
  });
  const message = await response.json();
  if (message.type !== 'scheduler_init') {
    throw new Error(`Invalid message type: ${message.type}.`);
  }
  return message.data;
};

export const createStreamClusterStatus = createHttpStreamFactory({
  url: `${API_BASE_URL}/cluster/status`,
  method: 'GET',
});
