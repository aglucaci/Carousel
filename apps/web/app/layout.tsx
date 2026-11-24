import type { Metadata } from "next";
import { Nav } from "@/components/Nav";
import "./globals.css";

export const metadata: Metadata = {
  title: "CAROUSEL",
  description: "Personalized scientific literature discovery",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <Nav />
        <main className="mx-auto w-full max-w-5xl px-4 pb-24 pt-6 sm:px-6">{children}</main>
      </body>
    </html>
  );
}
