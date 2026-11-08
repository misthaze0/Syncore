/**
 * 文件上传 Hook —— 封装拖拽 / 选择文件 / 上传状态
 */

import { useCallback, useState } from 'react';

import { useAgentStore, type UploadedFile } from '@/stores/agentStore';

export interface UseFileUploadResult {
  files: UploadedFile[];
  uploading: boolean;
  dragOver: boolean;
  error: string | null;
  /** 统一的上传入口（支持多文件） */
  uploadFiles: (files: FileList | File[]) => Promise<void>;
  remove: (name: string) => void;
  /** 绑定到拖拽区域的 props */
  dragHandlers: {
    onDragOver: (e: React.DragEvent) => void;
    onDragLeave: (e: React.DragEvent) => void;
    onDrop: (e: React.DragEvent) => void;
  };
}

/** 允许上传的扩展名 */
export const ACCEPTED_EXTENSIONS = [
  '.pdf', '.docx', '.doc', '.md', '.markdown', '.txt', '.csv',
  '.xlsx', '.xls', '.json', '.yaml', '.yml', '.py', '.js', '.ts', '.log',
];

export function useFileUpload(): UseFileUploadResult {
  const files = useAgentStore((s) => s.uploadedFiles);
  const uploading = useAgentStore((s) => s.uploading);
  const upload = useAgentStore((s) => s.upload);
  const remove = useAgentStore((s) => s.removeUploadedFile);

  const [dragOver, setDragOver] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const uploadFiles = useCallback(
    async (input: FileList | File[]) => {
      setError(null);
      const list = Array.from(input);
      for (const file of list) {
        try {
          await upload(file);
        } catch (e) {
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    },
    [upload],
  );

  const dragHandlers = {
    onDragOver: (e: React.DragEvent) => {
      e.preventDefault();
      setDragOver(true);
    },
    onDragLeave: (e: React.DragEvent) => {
      e.preventDefault();
      setDragOver(false);
    },
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      if (e.dataTransfer.files?.length) void uploadFiles(e.dataTransfer.files);
    },
  };

  return { files, uploading, dragOver, error, uploadFiles, remove, dragHandlers };
}
