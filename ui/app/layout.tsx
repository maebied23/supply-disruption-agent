import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Supply disruption approvals",
  description: "Approver screen for the Supply Disruption Response Agent (fictional data)",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
