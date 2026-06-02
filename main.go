package main

import "fmt"

func InitializeSystem() {
	ConnectDatabase()
	StartServer()
}

func ConnectDatabase() {
	fmt.Println("Connecting...")
}

func StartServer() {
	fmt.Println("Server started.")
}
