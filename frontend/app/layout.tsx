import type React from "react"
import type { Metadata } from "next"
import { Geist, Geist_Mono } from "next/font/google"
import { Analytics } from "@vercel/analytics/next"
import Script from "next/script"
import "./globals.css"
import { Header } from "@/components/header"
import { Footer } from "@/components/footer"
import { Toaster } from "@/components/ui/toaster"

const _geist = Geist({ subsets: ["latin"] })
const _geistMono = Geist_Mono({ subsets: ["latin"] })

export const metadata: Metadata = {
  title: "K-Shop - 한국 온라인 쇼핑몰",
  description: "최고의 상품을 합리적인 가격에 제공하는 온라인 쇼핑몰",
  generator: "v0.app",
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html lang="ko">
      <head>
        {/* <Script
          src="http://114.108.153.60:8080/ba.js"
          data-site="logflow"
          data-endpoint="http://114.108.153.60:8080/ba"
          data-click="true"
          data-scroll="true"
          data-spa="true"
          data-hb="15"
          data-sample="1.0"
          strategy="beforeInteractive"
          data-snapshot-upload="true"
          defer
        /> */}
        <Script
          src="https://54.180.149.234:8080/public/snapshot-sdk.js"
          data-endpoint="http://54.180.149.234:8080"
          data-viewports="1366x900,390x844"
          data-mode="auto" data-probe="on"
          defer
        />
      </head>
      <body className={`font-sans antialiased`}>
        <div className="flex min-h-screen flex-col">
          <Header />
          <main className="flex-1">{children}</main>
          <Footer />
        </div>
        <Toaster />
        <Analytics />
      </body>
    </html>
  )
}
