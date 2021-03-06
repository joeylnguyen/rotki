import { BigNumber } from 'bignumber.js';

interface ApiSupportedAsset {
  readonly active?: boolean;
  readonly ended?: number;
  readonly name: string;
  readonly started?: number;
  readonly symbol: string;
  readonly type: string;
}

export interface SupportedAssets {
  readonly [key: string]: ApiSupportedAsset;
}

export interface LimitedResponse<T> {
  readonly entries: T;
  readonly entriesFound: number;
  readonly entriesLimit: number;
}

export class TaskNotFoundError extends Error {}

export interface ActionResult<T> {
  readonly result: T;
  readonly message: string;
}

export interface AsyncQuery {
  readonly task_id: number;
}

export interface PendingTask {
  readonly taskId: number;
}

export interface Messages {
  readonly warnings: string[];
  readonly errors: string[];
}

export interface LocationData {
  readonly time: number;
  readonly location: string;
  readonly usd_value: string;
}

export interface Balance {
  readonly amount: BigNumber;
  readonly usdValue: BigNumber;
}

export interface HasBalance {
  readonly balance: Balance;
}

// This is equivalent to python's AssetBalance named tuple
export interface DBAssetBalance {
  readonly time: number;
  readonly asset: string;
  readonly amount: string;
  readonly usd_value: string;
}

export interface PeriodicClientQueryResult {
  readonly last_balance_save: number;
  readonly eth_node_connection: boolean;
  readonly history_process_start_ts: number;
  readonly history_process_current_ts: number;
}

export interface NetvalueDataResult {
  readonly times: number[];
  readonly data: number[];
}

export interface SingleAssetBalance {
  readonly time: number;
  readonly amount: string;
  readonly usd_value: string;
}

export interface VersionCheck {
  readonly our_version?: string;
  readonly latest_version?: string;
  readonly download_url?: string;
}
