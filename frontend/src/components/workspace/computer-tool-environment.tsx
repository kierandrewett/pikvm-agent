import {
  createContext,
  useContext,
  type PropsWithChildren,
} from "react";

export type ComputerToolEnvironment = {
  token?: string;
  runId?: string;
  machineName?: string;
  currentFrameId?: number;
  screenWidth?: number;
  screenHeight?: number;
  onOpenComputer?: () => void;
};

const ComputerToolEnvironmentContext = createContext<ComputerToolEnvironment>(
  {},
);

export function ComputerToolEnvironmentProvider({
  value,
  children,
}: PropsWithChildren<{ value: ComputerToolEnvironment }>) {
  return (
    <ComputerToolEnvironmentContext.Provider value={value}>
      {children}
    </ComputerToolEnvironmentContext.Provider>
  );
}

export const useComputerToolEnvironment = () =>
  useContext(ComputerToolEnvironmentContext);
