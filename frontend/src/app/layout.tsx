import type { Metadata } from "next";
import "./globals.css";
import "@xyflow/react/dist/style.css";
import { TooltipProvider } from "@/components/ui/tooltip";

export const metadata: Metadata = {
  title: "Ardberg · PR testing",
  description: "Agent-driven pull request testing with a visible execution flow.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en" className="font-sans"><body><TooltipProvider>{children}</TooltipProvider></body></html>;
}
